FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_ENV=production \
    APP_SECURE_COOKIES=true \
    APP_DATABASE_PATH=/data/agent.sqlite3

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && useradd --uid 10001 --create-home agent \
    && mkdir -p /data /run/browser-auth && chown agent:agent /data /run/browser-auth

COPY --chown=agent:agent backend ./backend
COPY --chown=agent:agent frontend ./frontend
USER agent
EXPOSE 8000
CMD ["python", "-m", "backend.app"]
