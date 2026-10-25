from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from .fake_model import ScriptedModel

TEST_PASSWORD = 'solo-para-pruebas-123456'


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    """Las pruebas nunca reutilizan credenciales reales de la máquina."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_client(tmp_path):
    stack = ExitStack()
    counter = 0

    def make(*, model='default', extras=(), path=None, **overrides):
        nonlocal counter
        counter += 1
        values = dict(ai_api_key='', app_login_password=TEST_PASSWORD, app_database_path=path or tmp_path / f'test-{counter}.sqlite3', app_allowed_hosts='testserver,localhost,127.0.0.1')
        values.update(overrides)
        settings = Settings(_env_file=None, **values)
        provider = ScriptedModel() if model == 'default' else model
        app = create_app(settings, model=provider, extra_tools=extras)
        client = stack.enter_context(TestClient(app, base_url='https://testserver' if settings.app_secure_cookies else 'http://testserver'))
        return client, app

    yield make
    stack.close()


def login(client, username='admin', password=TEST_PASSWORD):
    client.headers['origin'] = str(client.base_url).rstrip('/')
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    client.headers['x-csrf-token'] = response.json()['csrf_token']
    return response


def conversation(client):
    response = client.post('/api/conversations')
    assert response.status_code == 200, response.text
    return response.json()['id']
