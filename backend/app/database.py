import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import AppError


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, history TEXT NOT NULL DEFAULT '[]',
                    pending TEXT, busy INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL, events TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    day TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER, status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_owner_day ON runs(user_id, day);
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
            """)
            # Una ejecución interrumpida no se reanuda automáticamente: puede haber efectos externos.
            conn.execute("UPDATE conversations SET blocked=1 WHERE id IN (SELECT conversation_id FROM runs WHERE status='running')")
            conn.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            conn.execute("UPDATE conversations SET busy=0")
        os.chmod(path, 0o600)

    @contextmanager
    def connection(self, transaction=False):
        conn = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if transaction:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if transaction:
                conn.execute("COMMIT")
        except Exception:
            if transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def health(self):
        with self.connection() as conn:
            conn.execute("SELECT 1").fetchone()

    def owner(self):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id='owner'").fetchone()
            return dict(row) if row else None

    def configure_owner(self, username, password_hash):
        with self.connection(True) as conn:
            conn.execute("DELETE FROM auth_sessions")
            conn.execute("INSERT INTO users(id,username,password_hash) VALUES('owner',?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,password_hash=excluded.password_hash", (username, password_hash))

    def save_session(self, token_hash, user_id, expires):
        with self.connection(True) as conn:
            conn.execute("DELETE FROM auth_sessions WHERE expires<=?", (time.time(),))
            conn.execute("INSERT INTO auth_sessions VALUES(?,?,?)", (token_hash, user_id, expires))

    def session_user(self, token_hash):
        with self.connection() as conn:
            row = conn.execute("SELECT users.id,users.username FROM auth_sessions JOIN users ON users.id=auth_sessions.user_id WHERE token_hash=? AND expires>?", (token_hash, time.time())).fetchone()
            return dict(row) if row else None

    def delete_session(self, token_hash):
        with self.connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))

    def create_conversation(self, owner, maximum):
        now = time.time()
        identifier = str(uuid.uuid4())
        with self.connection(True) as conn:
            if conn.execute("SELECT COUNT(*) FROM conversations WHERE user_id=?", (owner,)).fetchone()[0] >= maximum:
                raise AppError(429, "conversation_limit", "Alcanzaste el límite de conversaciones. Elimina alguna para crear otra.")
            conn.execute("INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)", (identifier, owner, "Nueva conversación", now, now))
        return self.get_conversation(identifier, owner)

    def list_conversations(self, owner):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT id,title,created,updated,turns FROM conversations WHERE user_id=? ORDER BY updated DESC", (owner,)).fetchall()]

    def get_conversation(self, identifier, owner):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (identifier, owner)).fetchone()
            if not row:
                raise AppError(404, "not_found", "Conversación no encontrada.")
            result = dict(row)
            result["messages"] = [dict(item) for item in conn.execute("SELECT id,role,content,created,events FROM messages WHERE conversation_id=? ORDER BY id", (identifier,))]
            for message in result["messages"]:
                message["events"] = json.loads(message["events"])
            return result

    def delete_conversation(self, identifier, owner):
        with self.connection(True) as conn:
            row = conn.execute("SELECT busy FROM conversations WHERE id=? AND user_id=?", (identifier, owner)).fetchone()
            if not row:
                raise AppError(404, "not_found", "Conversación no encontrada.")
            if row["busy"]:
                raise AppError(409, "busy", "Espera a que termine la ejecución.")
            conn.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (identifier, owner))

    def reserve_run(self, identifier, owner, settings, *, approval_id=None):
        run_id = str(uuid.uuid4())
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connection(True) as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (identifier, owner)).fetchone()
            if not row:
                raise AppError(404, "not_found", "Conversación no encontrada.")
            if row["busy"]:
                raise AppError(409, "busy", "Esta conversación ya tiene una ejecución en curso.")
            if row['blocked']:
                raise AppError(409, 'execution_interrupted', 'La ejecución anterior quedó interrumpida. Crea una conversación nueva y comprueba cualquier efecto externo antes de repetir la acción.')
            pending = json.loads(row["pending"]) if row["pending"] else None
            if approval_id:
                if not pending or pending["id"] != approval_id or pending["expires"] < time.time():
                    raise AppError(409, "invalid_approval", "La confirmación caducó o ya fue utilizada.")
            elif pending:
                raise AppError(409, "approval_pending", "Confirma o rechaza la acción pendiente antes de continuar.")
            elif row["turns"] >= settings.max_conversation_turns:
                raise AppError(429, "turn_limit", "Esta conversación llegó a su límite. Crea una nueva.")
            used = conn.execute("SELECT COUNT(*),COALESCE(SUM(COALESCE(actual,reserved)),0) FROM runs WHERE user_id=? AND day=?", (owner, day)).fetchone()
            if used[0] >= settings.max_daily_runs or used[1] + settings.token_reservation > settings.max_daily_tokens:
                raise AppError(429, "daily_limit", "Alcanzaste el límite diario de ejecuciones o la reserva de tokens. Se restablece a las 00:00 UTC.")
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,NULL,'running')", (run_id, owner, identifier, day, settings.token_reservation))
            # Consumir la aprobación ANTES de reanudar evita su repetición, incluso si falla.
            conn.execute("UPDATE conversations SET busy=1,pending=NULL WHERE id=?", (identifier,))
            return run_id, dict(row), pending

    def finish_run(self, identifier, run_id, *, history=None, pending=None, user_text=None, assistant_text=None, events=None, actual=None, failed=False, block=False):
        with self.connection(True) as conn:
            conn.execute("UPDATE runs SET actual=?,status=? WHERE id=?", (actual, "failed" if failed else "complete", run_id))
            now = time.time()
            if user_text is not None:
                conn.execute("INSERT INTO messages(conversation_id,role,content,created) VALUES(?,?,?,?)", (identifier, "user", user_text, now))
            if assistant_text is not None:
                conn.execute("INSERT INTO messages(conversation_id,role,content,created,events) VALUES(?,?,?,?,?)", (identifier, "error" if failed else "assistant", assistant_text, now, json.dumps(events or [], ensure_ascii=False)))
            if failed:
                conn.execute("UPDATE conversations SET busy=0,updated=?,blocked=? WHERE id=?", (now, int(block), identifier))
            else:
                conn.execute("UPDATE conversations SET busy=0,updated=?,history=?,pending=?,turns=turns+1 WHERE id=?", (now, json.dumps(history, ensure_ascii=False), json.dumps(pending, ensure_ascii=False) if pending else None, identifier))

    def usage(self, owner):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*),COALESCE(SUM(COALESCE(actual,reserved)),0) FROM runs WHERE user_id=? AND day=?", (owner, day)).fetchone()
            return {"runs": row[0], "tokens_accounted": row[1], "day_utc": day}
