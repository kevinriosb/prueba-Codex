import asyncio
import json
import time
import uuid

import pytest
from agents import function_tool

from backend.app.config import Settings
from backend.app.errors import AppError
from backend.app.tools.registry import ToolSpec
from .conftest import TEST_PASSWORD, conversation, login
from .fake_model import ScriptedModel, answer, call


def test_budget_reserved_before_model(make_client):
    model = ScriptedModel()
    client, app = make_client(model=model, max_daily_tokens=1000)
    login(client)
    identifier = conversation(client)
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Hola'})
    assert response.status_code == 429
    assert not model.inputs
    assert app.state.database.usage('owner')['runs'] == 0


def test_timeout_releases_conversation_and_accounts_uncertain_usage(make_client):
    client, app = make_client(model=ScriptedModel(pause=10), run_timeout_seconds=5)
    login(client)
    identifier = conversation(client)
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Hola'})
    assert response.status_code == 504
    assert app.state.database.get_conversation(identifier, 'owner')['busy'] == 0
    assert app.state.database.usage('owner')['tokens_accounted'] == app.state.settings.token_reservation


async def test_concurrent_requests_are_rejected_not_queued(make_client):
    client, app = make_client(model=ScriptedModel(pause=.1))
    login(client)
    identifier = conversation(client)
    first = asyncio.create_task(app.state.service.execute(identifier, 'owner', text='Primero'))
    await asyncio.sleep(.02)
    with pytest.raises(AppError) as caught:
        await app.state.service.execute(identifier, 'owner', text='Segundo')
    assert caught.value.code == 'server_busy'
    await first


def test_context_size_limit_is_bytes_not_just_characters(make_client):
    model = ScriptedModel()
    client, _ = make_client(model=model, max_context_bytes=4000)
    login(client)
    response = client.post(f'/api/conversations/{conversation(client)}/messages', json={'text': 'ó' * 2100})
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'context_limit'
    assert not model.inputs


def test_tool_call_limit(make_client):
    model = ScriptedModel([[call('calculator', expression='1+1')] for _ in range(5)])
    client, _ = make_client(model=model, max_tool_calls=2, max_agent_turns=4)
    login(client)
    response = client.post(f'/api/conversations/{conversation(client)}/messages', json={'text': 'Calcula muchas veces'})
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'tool_limit'


def test_model_cannot_bypass_disabled_parallel_tool_calls(make_client):
    model = ScriptedModel([[call('calculator', expression='1+1') for _ in range(5)]])
    client, _ = make_client(model=model, max_tool_calls=2)
    login(client)
    response = client.post(f'/api/conversations/{conversation(client)}/messages', json={'text': 'Calcula muchas veces'})
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'tool_limit'


def test_unknown_tools_cannot_execute(make_client):
    model = ScriptedModel([[call('run_shell', command='id')]])
    client, _ = make_client(model=model)
    login(client)
    response = client.post(f'/api/conversations/{conversation(client)}/messages', json={'text': 'Ejecuta un comando'})
    assert response.status_code == 502


def write_spec(writes):
    async def external_change(note: str) -> str:
        """Acción ficticia de prueba, sin conexiones externas."""
        writes.append(note)
        return 'ok'
    return ToolSpec(external_change, 'write')


def test_pending_approval_survives_restart_and_does_not_include_credentials(make_client, tmp_path):
    writes = []
    path = tmp_path / 'approval.sqlite3'
    spec = write_spec(writes)
    client, app = make_client(path=path, model=ScriptedModel([[call('external_change', note='original')]]), extras=(spec,))
    login(client)
    identifier = conversation(client)
    result = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Solicitar acción'}).json()
    assert result['approval'] and writes == []
    stored = app.state.database.get_conversation(identifier, 'owner')['pending']
    assert TEST_PASSWORD not in stored
    second, _ = make_client(path=path, model=ScriptedModel([[answer('Confirmada después de reiniciar.')]]), extras=(spec,))
    login(second)
    response = second.post(f'/api/conversations/{identifier}/approvals/{result["approval"]["id"]}', json={'approved': True})
    assert response.status_code == 200, response.text
    assert writes == ['original']


def test_approval_expiry_and_conversation_binding(make_client):
    writes = []
    client, app = make_client(model=ScriptedModel([[call('external_change', note='nota')]]), extras=(write_spec(writes),))
    login(client)
    first = conversation(client)
    result = client.post(f'/api/conversations/{first}/messages', json={'text': 'Acción'}).json()
    approval_id = result['approval']['id']
    other = conversation(client)
    assert client.post(f'/api/conversations/{other}/approvals/{approval_id}', json={'approved': True}).status_code == 409
    row = app.state.database.get_conversation(first, 'owner')
    pending = json.loads(row['pending'])
    pending['expires'] = time.time() - 1
    with app.state.database.connection() as conn:
        conn.execute('UPDATE conversations SET pending=? WHERE id=?', (json.dumps(pending), first))
    assert client.post(f'/api/conversations/{first}/approvals/{approval_id}', json={'approved': True}).status_code == 409
    assert writes == []


def test_failure_after_approval_blocks_replay_and_requires_reconciliation(make_client):
    writes = []
    model = ScriptedModel([[call('external_change', note='efecto')]])
    client, app = make_client(model=model, extras=(write_spec(writes),))
    login(client)
    identifier = conversation(client)
    pending = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Acción'}).json()['approval']
    model.failure = RuntimeError('Fallo después de la herramienta')
    url = f'/api/conversations/{identifier}/approvals/{pending["id"]}'
    assert client.post(url, json={'approved': True}).status_code == 502
    assert writes == ['efecto']
    assert client.post(url, json={'approved': True}).status_code == 409
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Repite'}).status_code == 409
    assert writes == ['efecto']


def test_secure_production_settings_and_masked_configuration_errors():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_login_password=TEST_PASSWORD, app_env='production', app_secure_cookies=False)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, app_login_password='short')
    assert 'input_value' not in str(caught.value)


def test_expired_session_is_rejected(make_client):
    client, app = make_client()
    login(client)
    with app.state.database.connection() as conn:
        conn.execute('UPDATE auth_sessions SET expires=0')
    assert client.get('/api/auth/session').status_code == 401


def test_password_rotation_revokes_old_sessions(make_client, tmp_path):
    path = tmp_path / 'password-rotation.sqlite3'
    old, _ = make_client(path=path)
    login(old)
    newer, _ = make_client(path=path, app_login_password='otra-contraseña-privada-de-prueba')
    assert old.get('/api/auth/session').status_code == 401
    login(newer, password='otra-contraseña-privada-de-prueba')
    assert newer.get('/api/auth/session').status_code == 200


async def test_chunked_request_is_bounded_before_json_parsing(make_client):
    import httpx
    client, app = make_client()

    async def body():
        for _ in range(3):
            yield b'x' * 10000

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as caller:
        response = await caller.post('/api/auth/login', headers={'Origin': 'http://testserver'}, content=body())
    assert response.status_code == 413


def test_dotenv_configures_network_and_model(tmp_path):
    env = tmp_path / '.env'
    env.write_text('APP_LOGIN_PASSWORD=solo-para-pruebas-env\nHOST=0.0.0.0\nPORT=8123\nAI_MODEL=modelo-elegido\n')
    settings = Settings(_env_file=env)
    assert settings.host == '0.0.0.0'
    assert settings.port == 8123
    assert settings.ai_model == 'modelo-elegido'
