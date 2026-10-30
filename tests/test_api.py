import json
import logging
import uuid

import pytest
from agents import function_tool

from backend.app.auth import COOKIE_NAME
from backend.app.tools.registry import ToolSpec
from .conftest import TEST_PASSWORD, conversation, login
from .fake_model import ScriptedModel, answer, call


def test_health_and_private_routes(make_client):
    client, app = make_client()
    assert client.get('/health').json()['status'] == 'ok'
    for path in ['/api/config', '/api/conversations', '/api/auth/session']:
        assert client.get(path).status_code == 401
    assert client.get('/').status_code == 200
    assert client.get('/assets/app.js').status_code == 200
    assert client.get('/assets/.env').status_code == 404


def test_authentication_cookie_csrf_logout(make_client):
    client, _ = make_client()
    response = login(client)
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    token = client.cookies.get(COOKIE_NAME)
    assert token not in response.text
    csrf = client.headers.pop('x-csrf-token')
    assert client.post('/api/conversations').status_code == 403
    client.headers['x-csrf-token'] = csrf
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/auth/session').status_code == 401


def test_login_bruteforce_and_no_leaked_values(make_client):
    client, _ = make_client()
    client.headers['origin'] = 'http://testserver'
    for _ in range(5):
        response = client.post('/api/auth/login', json={'username': 'admin', 'password': 'incorrecta'})
        assert response.status_code == 401
        assert 'incorrecta' not in response.text
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': TEST_PASSWORD}).status_code == 429


def test_origin_host_and_security_headers(make_client):
    client, _ = make_client()
    response = client.get('/')
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert client.post('/api/auth/login', headers={'Origin': 'https://attacker.invalid'}, json={'username': 'admin', 'password': TEST_PASSWORD}).status_code == 403
    assert client.get('/health', headers={'host': 'attacker.invalid'}).status_code == 400


def test_production_cookie(make_client):
    client, _ = make_client(app_env='production', app_secure_cookies=True)
    assert 'secure' in login(client).headers['set-cookie'].lower()
    assert client.get('/docs').status_code == 404
    assert 'strict-transport-security' in client.get('/').headers


def test_calculator_sdk_integration_and_history(make_client):
    model = ScriptedModel([[call('calculator', expression='500 * 1.15 / 2')], [answer('Cada persona paga 287.50 pesos.')], [answer('La cuenta anterior era de 500 pesos.')]])
    client, app = make_client(model=model)
    login(client)
    identifier = conversation(client)
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Divide 500 con 15 % entre 2.'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert '287.50' in result['messages'][-1]['content']
    assert result['messages'][-1]['events'][0]['tool'] == 'calculator'
    assert '287.5' in result['messages'][-1]['events'][0]['result']
    assert client.get(f'/api/conversations/{identifier}').json()['messages'] == result['messages']
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': '¿Cuánto era la cuenta anterior?'})
    assert response.status_code == 200
    assert 'Divide 500' in json.dumps(model.inputs[-1])
    assert app.state.database.usage('owner')['tokens_accounted'] == 180


def test_clock_sdk_tool(make_client):
    client, _ = make_client(model=ScriptedModel([[call('current_server_time')], [answer('Hora consultada en el servidor.')]]))
    login(client)
    identifier = conversation(client)
    result = client.post(f'/api/conversations/{identifier}/messages', json={'text': '¿Qué hora es?'}).json()
    assert result['messages'][-1]['events'][0]['tool'] == 'current_server_time'
    assert 'America/Mexico_City' in result['messages'][-1]['events'][0]['result']


def test_no_key_fails_closed_without_budget_consumption(make_client):
    client, app = make_client(model=None)
    login(client)
    identifier = conversation(client)
    assert client.get('/api/config').json()['ai_configured'] is False
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Hola'})
    assert response.status_code == 503
    assert app.state.database.usage('owner')['runs'] == 0


def test_user_cannot_read_or_modify_other_conversations(make_client):
    client, app = make_client()
    login(client)
    db = app.state.database
    with db.connection() as conn:
        conn.execute("INSERT INTO users VALUES('other','other','unused-test-hash')")
    other = db.create_conversation('other', 30)['id']
    assert client.get(f'/api/conversations/{other}').status_code == 404
    assert client.post(f'/api/conversations/{other}/messages', json={'text': 'hola'}).status_code == 404
    assert client.request('DELETE', f'/api/conversations/{other}', json={'confirmed': True}).status_code == 404


def test_message_size_unknown_fields_and_body_limit(make_client):
    client, _ = make_client()
    login(client)
    identifier = conversation(client)
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text': 'x' * 4001}).status_code == 422
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text': 'hola', 'tool': 'shell'}).status_code == 422
    assert client.post('/api/auth/login', content=b'x' * 25000).status_code == 413


def test_daily_limit_cannot_be_reset_by_deleting_history(make_client):
    client, _ = make_client(max_daily_runs=1)
    login(client)
    identifier = conversation(client)
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text': 'hola'}).status_code == 200
    assert client.request('DELETE', f'/api/conversations/{identifier}', json={'confirmed': True}).status_code == 200
    second = conversation(client)
    assert client.post(f'/api/conversations/{second}/messages', json={'text': 'hola'}).status_code == 429


def test_errors_and_audit_never_echo_credentials_or_prompts(make_client, caplog):
    secret = 'credential-that-must-not-be-logged'
    prompt = 'mensaje-privado-que-no-debe-aparecer-en-logs'
    client, app = make_client(model=ScriptedModel(failure=RuntimeError(secret)))
    caplog.set_level(logging.INFO, logger='personal_agent.audit')
    login(client)
    identifier = conversation(client)
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': prompt})
    assert response.status_code == 502
    assert secret not in response.text and secret not in caplog.text
    assert prompt not in caplog.text and TEST_PASSWORD not in caplog.text
    assert app.state.settings.failed_run_floor_tokens <= app.state.database.usage('owner')['tokens_accounted'] == min(app.state.settings.token_reservation, max(app.state.settings.failed_run_floor_tokens, len(json.dumps([{'role':'user','content':prompt}], ensure_ascii=False).encode()) // 3)) < app.state.settings.token_reservation


def test_max_turns_stops_tool_loop(make_client):
    model = ScriptedModel([[call('calculator', expression='1+1')] for _ in range(5)])
    client, _ = make_client(model=model, max_agent_turns=2)
    login(client)
    response = client.post(f'/api/conversations/{conversation(client)}/messages', json={'text': 'Repite sin parar'})
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'agent_turn_limit'


def test_confirmation_required_then_approve_once(make_client):
    writes = []

    async def external_change(note: str) -> str:
        """Escritura ficticia exclusivamente para probar la aprobación."""
        writes.append(note)
        return 'Escritura local de prueba completada.'

    model = ScriptedModel([[call('external_change', note='nota de prueba')], [answer('Acción confirmada y ejecutada.')]])
    client, app = make_client(model=model, extras=(ToolSpec(external_change, 'write'),))
    login(client)
    identifier = conversation(client)
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Haz una escritura de prueba'})
    assert response.status_code == 200, response.text
    pending = response.json()['approval']
    assert pending and not writes
    assert 'state' not in pending
    assert pending['tools'][0]['name'] == 'external_change'
    url = f'/api/conversations/{identifier}/approvals/{pending["id"]}'
    assert client.post(url, json={'approved': True, 'arguments': {'note': 'cambiada'}}).status_code == 422
    assert not writes
    accepted = client.post(url, json={'approved': True})
    assert accepted.status_code == 200, accepted.text
    assert writes == ['nota de prueba']
    assert client.post(url, json={'approved': True}).status_code == 409
    assert writes == ['nota de prueba']


def test_rejected_tool_never_executes(make_client):
    writes = []

    async def external_change(note: str) -> str:
        """Escritura ficticia."""
        writes.append(note)
        return 'ok'

    model = ScriptedModel([[call('external_change', note='no ejecutar')], [answer('No se ejecutó la acción.')]])
    client, _ = make_client(model=model, extras=(ToolSpec(external_change, 'write'),))
    login(client)
    identifier = conversation(client)
    pending = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'acción'}).json()['approval']
    response = client.post(f'/api/conversations/{identifier}/approvals/{pending["id"]}', json={'approved': False})
    assert response.status_code == 200, response.text
    assert writes == []


def test_persistent_history_after_restart(make_client, tmp_path):
    path = tmp_path / 'persistent.sqlite3'
    client, _ = make_client(path=path)
    login(client)
    identifier = conversation(client)
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Recuerda esta conversación'}).status_code == 200
    second, _ = make_client(path=path)
    login(second)
    assert len(second.get(f'/api/conversations/{identifier}').json()['messages']) == 2


def test_deletion_requires_confirmation(make_client):
    client, _ = make_client()
    login(client)
    identifier = conversation(client)
    assert client.request('DELETE', f'/api/conversations/{identifier}', json={'confirmed': False}).status_code == 422
    assert client.get(f'/api/conversations/{identifier}').status_code == 200
