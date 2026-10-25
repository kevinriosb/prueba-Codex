import asyncio
import hashlib
import secrets
import time
from collections import deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

from .audit import audit
from .errors import AppError

COOKIE_NAME = "agent_session"
hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return digest("csrf:" + token)


def verify_password(encoded, supplied):
    try:
        return hasher.verify(encoded, supplied)
    except (VerificationError, InvalidHashError):
        return False


class Auth:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings
        existing = database.owner()
        password = settings.app_login_password.get_secret_value()
        if not existing or existing["username"] != settings.app_login_username or not verify_password(existing["password_hash"], password):
            database.configure_owner(settings.app_login_username, hasher.hash(password))
        self.attempts = {}
        self.global_attempts = deque()
        self.hash_lock = asyncio.Lock()

    def check_rate(self, address):
        now = time.monotonic()
        # Límite global acota además la memoria de direcciones y el trabajo de Argon2.
        for queue in [self.global_attempts]:
            while queue and now - queue[0] > 300:
                queue.popleft()
        self.attempts = {key: value for key, value in self.attempts.items() if value and now - value[-1] <= 300}
        queue = self.attempts.setdefault(address, deque())
        while queue and now - queue[0] > 300:
            queue.popleft()
        if len(queue) >= 5 or len(self.global_attempts) >= 30 or self.hash_lock.locked():
            raise AppError(429, "login_rate_limit", "Demasiados intentos. Espera unos minutos.")
        queue.append(now)
        self.global_attempts.append(now)

    async def login(self, username, password, address):
        self.check_rate(address)
        async with self.hash_lock:
            owner = self.database.owner()
            valid_password = await asyncio.to_thread(verify_password, owner["password_hash"], password)
            valid_username = secrets.compare_digest(username.encode(), owner["username"].encode())
            if not valid_password or not valid_username:
                audit("login_failed")
                raise AppError(401, "invalid_login", "Usuario o contraseña incorrectos.")
            token = secrets.token_urlsafe(32)
            self.database.save_session(digest(token), owner["id"], time.time() + self.settings.app_session_hours * 3600)
            audit("login_success")
            return token


def authenticated(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    if len(token) > 128:
        raise AppError(401, "authentication_required", "Inicia sesión para continuar.")
    user = request.app.state.database.session_user(digest(token)) if token else None
    if not user:
        raise AppError(401, "authentication_required", "Inicia sesión para continuar.")
    return user


def check_csrf(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    supplied = request.headers.get("x-csrf-token", "")
    if not token or not secrets.compare_digest(csrf_for(token).encode(), supplied.encode()):
        raise AppError(403, "csrf", "Actualiza la página antes de continuar.")
