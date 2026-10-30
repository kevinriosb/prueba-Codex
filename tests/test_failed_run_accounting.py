"""La admisión reserva tokens; un fallo contabiliza consumo y no toda la reserva."""
import json

import pytest
from agents import ModelResponse, Usage

from backend.app.config import Settings
from .conftest import TEST_PASSWORD, conversation, login
from .fake_model import ScriptedModel, answer, call
from .test_billing import billing_client
from .test_telegram_agent import create, owner


class PartialFailure(ScriptedModel):
    def __init__(self):
        super().__init__([[call('calculator', expression='1+1')] for _ in range(3)] + [[answer('Listo.')]])
        self.fail_next = False

    async def get_response(self, *args, **kwargs):
        if self.fail_next:
            self.inputs.append(args[1] if len(args)>1 else kwargs['input'])
            self.fail_next = False
            raise RuntimeError('Fallo transitorio simulado tras 60 tokens conocidos')
        self.fail_next = True
        return await super().get_response(*args, **kwargs)


def test_three_partially_measured_failures_allow_a_fourth_run(make_client):
    model = PartialFailure()
    client, app = make_client(model=model)
    login(client)
    identifier = conversation(client)
    expected = []
    for _ in range(3):
        response = client.post(f'/api/conversations/{identifier}/messages', json={'text':'Calcula'})
        assert response.status_code == 502
        expected.append(min(app.state.settings.token_reservation,
            60 + len(json.dumps(model.inputs[-1], ensure_ascii=False).encode()) // 3))
    response = client.post(f'/api/conversations/{identifier}/messages', json={'text':'Ahora responde'})
    assert response.status_code == 200, response.text
    assert app.state.database.usage('owner')['tokens_accounted'] == sum(expected) + 60
    with app.state.database.connection() as conn:
        rows = conn.execute("SELECT actual FROM runs WHERE status='failed' ORDER BY rowid").fetchall()
    assert [row['actual'] for row in rows] == expected


def test_large_successful_usage_is_not_capped_and_still_blocks_admission(make_client):
    class LargeUsage(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            result = await super().get_response(*args, **kwargs)
            return ModelResponse(output=result.output, response_id=result.response_id,
                usage=Usage(requests=1, input_tokens=240000, output_tokens=10001, total_tokens=250001))

    model = LargeUsage()
    client, app = make_client(model=model)
    login(client)
    identifier = conversation(client)
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text':'Hola'}).status_code == 200
    assert app.state.database.usage('owner')['tokens_accounted'] == 250001
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text':'Otra vez'}).status_code == 429
    assert len(model.inputs) == 1


@pytest.mark.parametrize('floor,text',[(0,'Hola'),(2000,'Hola'),(2000,'界'*2100),(100000,'Hola')], ids=['zero','default','utf8','capped'])
def test_unmeasured_failure_uses_utf8_input_estimate_and_capped_floor(make_client, floor, text):
    model = ScriptedModel(failure=RuntimeError('Fallo antes de conocer usage'))
    client, app = make_client(model=model, failed_run_floor_tokens=floor)
    login(client)
    identifier = conversation(client)
    assert client.post(f'/api/conversations/{identifier}/messages', json={'text':text}).status_code == 502
    estimate = len(json.dumps(model.inputs[-1], ensure_ascii=False).encode()) // 3
    expected = min(app.state.settings.token_reservation, max(floor, estimate))
    assert app.state.database.usage('owner')['tokens_accounted'] == expected


@pytest.mark.parametrize('floor',[0,1234,100000])
@pytest.mark.parametrize('status',['running','interrupted','failed'])
def test_restart_accounts_unmeasured_runs_at_floor_and_preserves_actual_usage(make_client, tmp_path, floor, status):
    path = tmp_path / 'restart-accounting.sqlite3'
    client, app = make_client(path=path, failed_run_floor_tokens=floor)
    login(client)
    identifier = conversation(client)
    run_id, _, _ = app.state.database.reserve_run(identifier, 'owner', app.state.settings)
    if status != 'running':
        with app.state.database.connection() as conn:
            conn.execute('UPDATE runs SET status=? WHERE id=?', (status,run_id))
    measured = app.state.database.create_conversation('owner', 30)
    completed_id, _, _ = app.state.database.reserve_run(measured['id'], 'owner', app.state.settings)
    app.state.database.finish_run(measured['id'], completed_id, history=[], actual=200)
    _, restarted = make_client(path=path, failed_run_floor_tokens=floor)
    expected = min(app.state.settings.token_reservation, floor)
    assert restarted.state.database.usage('owner')['tokens_accounted'] == expected + 200
    with restarted.state.database.connection() as conn:
        row = conn.execute('SELECT status,actual FROM runs WHERE id=?',(run_id,)).fetchone()
    assert row['actual'] == expected
    assert row['status'] == ('interrupted' if status=='running' else status)
    if status == 'running':
        assert restarted.state.database.get_conversation(identifier, 'owner')['blocked'] == 1


def test_limits_show_the_accounted_failed_run_cost(billing_client):
    model = ScriptedModel(failure=RuntimeError('Proveedor temporalmente caído'))
    _, app, _, _, _, telegram, chat = create(billing_client, model)
    chat('Hola')
    usage = app.state.database.usage(owner(app))
    assert usage['tokens_accounted'] == 2000
    chat('/limites')
    assert 'reserva de tokens 2000/250000' in telegram.messages[-1][1]


def test_failed_run_floor_setting_has_default_and_bounds():
    from pydantic import ValidationError

    assert Settings(_env_file=None, app_login_password=TEST_PASSWORD).failed_run_floor_tokens == 2000
    for value in (-1,100001):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, app_login_password=TEST_PASSWORD, failed_run_floor_tokens=value)
