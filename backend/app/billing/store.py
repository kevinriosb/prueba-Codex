import base64
import hashlib
import hmac
import json
import os
import time
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import AppError


class BillingStore:
    """Una envoltura cifrada por registro; propietario, tipo e ID forman el AAD."""
    def __init__(self, database, encoded_key):
        try:
            key = base64.b64decode(encoded_key, altchars=b'-_', validate=True)
            if len(key) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise AppError(503, 'billing_key', 'BILLING_ENCRYPTION_KEY debe ser una clave base64 de 32 bytes.') from None
        self.database, self.cipher, self.key = database, AESGCM(key), key
        with database.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS billing_records(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
                    payload BLOB NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    expires REAL, lookup TEXT, state TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(kind,owner,lookup));
                CREATE INDEX IF NOT EXISTS billing_owner ON billing_records(owner,kind);
                CREATE TABLE IF NOT EXISTS billing_extractions(owner TEXT NOT NULL, day TEXT NOT NULL,
                    document_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(owner,document_id));
                CREATE TABLE IF NOT EXISTS billing_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL, record_id TEXT NOT NULL, event TEXT NOT NULL, created REAL NOT NULL);
            ''')

    def digest(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def _pack(self, kind, owner, identifier, payload):
        nonce = os.urandom(12)
        aad = f'{kind}:{owner}:{identifier}'.encode()
        return nonce + self.cipher.encrypt(nonce, json.dumps(payload, ensure_ascii=False).encode(), aad)

    def _read(self, row):
        try:
            payload = row['payload']
            value = json.loads(self.cipher.decrypt(payload[:12], payload[12:],
                f"{row['kind']}:{row['owner']}:{row['id']}".encode()))
        except (InvalidTag, ValueError, TypeError):
            raise AppError(503, 'billing_decryption', 'No se puede abrir el registro fiscal con la clave configurada.') from None
        return value | {'id':row['id'], '_version':row['version'], '_state':row['state'], '_expires':row['expires']}

    def add(self, kind, owner, payload, *, expires=None, lookup=None, state='active'):
        identifier, now = str(uuid.uuid4()), time.time()
        with self.database.connection(True) as conn:
            conn.execute('INSERT INTO billing_records(id,kind,owner,payload,created,updated,expires,lookup,state) VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,kind,owner,self._pack(kind,owner,identifier,payload),now,now,expires,lookup,state))
        return self.get(kind,owner,identifier)

    def get(self, kind, owner, identifier):
        with self.database.connection() as conn:
            row = conn.execute('SELECT * FROM billing_records WHERE kind=? AND owner=? AND id=? AND (expires IS NULL OR expires>?)',
                (kind,owner,identifier,time.time())).fetchone()
        if not row:
            raise AppError(404, 'billing_not_found', 'Registro no encontrado, eliminado o caducado.')
        return self._read(row)

    def find(self, kind, owner, lookup):
        with self.database.connection() as conn:
            row = conn.execute('SELECT * FROM billing_records WHERE kind=? AND owner=? AND lookup=? AND (expires IS NULL OR expires>?)',
                (kind,owner,lookup,time.time())).fetchone()
        return self._read(row) if row else None

    def find_oauth_state(self, lookup):
        with self.database.connection() as conn:
            row=conn.execute("SELECT * FROM billing_records WHERE kind='oauth_state' AND lookup=? AND expires>?",(lookup,time.time())).fetchone()
        if not row:
            raise AppError(400,'oauth_state','La conexión de Gmail caducó o no es válida.')
        return row['owner'],self._read(row)

    def list(self, kind, owner):
        with self.database.connection() as conn:
            rows = conn.execute('SELECT * FROM billing_records WHERE kind=? AND owner=? AND (expires IS NULL OR expires>?) ORDER BY created DESC',
                (kind,owner,time.time()))
            result=[]
            for row in rows:
                record=self._read(row)
                # No retener todos los archivos descifrados al listar metadatos.
                if kind=='document':
                    record={key:value for key,value in record.items() if key not in {'data','extracted'}}
                result.append(record)
            return result

    def count(self,kind,owner):
        with self.database.connection() as conn:
            return conn.execute('SELECT COUNT(*) FROM billing_records WHERE kind=? AND owner=? AND (expires IS NULL OR expires>?)',(kind,owner,time.time())).fetchone()[0]

    def update(self, kind, owner, identifier, payload, *, version=None, state=None):
        clean = {key:value for key,value in payload.items() if key!='id' and not key.startswith('_')}
        with self.database.connection(True) as conn:
            row = conn.execute('SELECT * FROM billing_records WHERE kind=? AND owner=? AND id=?', (kind,owner,identifier)).fetchone()
            if not row or (version is not None and row['version']!=version):
                raise AppError(409, 'billing_changed', 'El registro cambió. Revisa de nuevo los datos.')
            conn.execute('UPDATE billing_records SET payload=?,updated=?,version=version+1,state=? WHERE id=?',
                (self._pack(kind,owner,identifier,clean),time.time(),state or row['state'],identifier))
        return self.get(kind,owner,identifier)

    def claim(self, kind, owner, identifier):
        with self.database.connection(True) as conn:
            row = conn.execute('SELECT * FROM billing_records WHERE kind=? AND owner=? AND id=?', (kind,owner,identifier)).fetchone()
            if not row or row['state']!='pending' or (row['expires'] and row['expires']<=time.time()):
                raise AppError(409, 'billing_approval', 'La autorización caducó o ya fue utilizada.')
            conn.execute("UPDATE billing_records SET state='claimed',version=version+1,updated=?,expires=CASE WHEN kind='email_approval' THEN NULL ELSE expires END WHERE id=?",(time.time(),identifier))
        return self._read(row)

    def expire_at(self,kind,owner,identifier,expires):
        # No recuperar registros vencidos ni cambiar propietario/tipo.
        self.get(kind,owner,identifier)
        with self.database.connection(True) as conn:
            conn.execute('UPDATE billing_records SET expires=? WHERE kind=? AND owner=? AND id=? AND (expires IS NULL OR expires>?)',
                (expires,kind,owner,identifier,time.time()))
        return self.get(kind,owner,identifier)

    def delete(self, kind, owner, identifier):
        self.get(kind,owner,identifier)
        with self.database.connection(True) as conn:
            conn.execute('PRAGMA secure_delete=ON')
            conn.execute('DELETE FROM billing_records WHERE kind=? AND owner=? AND id=?',(kind,owner,identifier))

    def change_lookup(self,kind,owner,identifier,lookup):
        with self.database.connection(True) as conn:
            conn.execute('UPDATE billing_records SET lookup=? WHERE kind=? AND owner=? AND id=?',(lookup,kind,owner,identifier))

    def tracking_tickets(self):
        with self.database.connection() as conn:
            rows=conn.execute("SELECT * FROM billing_records WHERE kind='ticket' AND state='tracking' ORDER BY updated LIMIT 20").fetchall()
        return [(row['owner'],self._read(row)) for row in rows]

    def purge(self):
        with self.database.connection(True) as conn:
            conn.execute('PRAGMA secure_delete=ON')
            count = conn.execute('DELETE FROM billing_records WHERE expires IS NOT NULL AND expires<=?',(time.time(),)).rowcount
        return count

    def audit(self, owner, identifier, event):
        with self.database.connection(True) as conn:
            conn.execute('INSERT INTO billing_audit(owner,record_id,event,created) VALUES(?,?,?,?)',(owner,identifier,event,time.time()))
