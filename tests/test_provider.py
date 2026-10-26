import json

import httpx2
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from backend.app.tools.registry import ToolSpec

from .conftest import conversation, login
from .fake_model import answer, call


async def test_real_responses_adapter_without_network_or_charges(make_client):
    requests = []
    outputs = [[call('calculator', expression='500 * 1.15 / 2')], [answer('Cada persona paga 287.50 pesos.')]]

    def transport(request):
        assert request.url == 'https://api.openai.com/v1/responses'
        assert request.method == 'POST'
        body = json.loads(request.content)
        assert body['store'] is False
        assert body['max_output_tokens'] == 150
        assert body['parallel_tool_calls'] is False
        requests.append(body)
        return httpx2.Response(200, json={
            'id': 'resp_local_' + str(len(requests)), 'object': 'response', 'created_at': 1,
            'status': 'completed', 'model': 'gpt-4.1-mini',
            'output': [item.model_dump() for item in outputs.pop(0)],
            'usage': {'input_tokens': 40, 'output_tokens': 20, 'total_tokens': 60,
                'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}},
            'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': body['tools'],
            'error': None, 'incomplete_details': None, 'metadata': {}, 'instructions': body.get('instructions'),
        })

    async with AsyncOpenAI(api_key='clave-ficticia-solo-pruebas', max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))) as provider:
        model = OpenAIResponsesModel(model='gpt-4.1-mini', openai_client=provider)
        client, _ = make_client(model=model)
        login(client)
        identifier = conversation(client)
        response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Divide la cuenta con propina'})
        assert response.status_code == 200, response.text
        assert '287.50' in response.json()['messages'][-1]['content']
        assert 'clave-ficticia' not in response.text
        assert len(requests) == 2
        assert any(item.get('type') == 'function_call_output' for item in requests[1]['input'])


async def test_provider_key_never_enters_persisted_approval_state(make_client):
    key = 'credencial-ficticia-que-no-debe-guardarse'

    async def write_example(note: str) -> str:
        """Acción ficticia pendiente de autorización."""
        raise AssertionError('No debe ejecutarse antes de aprobar')

    def transport(request):
        return httpx2.Response(200, json={
            'id': 'resp_approval_local', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'gpt-4.1-mini', 'output': [call('write_example', note='prueba').model_dump()],
            'usage': {'input_tokens': 40, 'output_tokens': 20, 'total_tokens': 60,
                'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}},
        })

    async with AsyncOpenAI(api_key=key, max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))) as provider:
        model = OpenAIResponsesModel(model='gpt-4.1-mini', openai_client=provider)
        client, app = make_client(model=model, extras=(ToolSpec(write_example, 'write'),))
        login(client)
        identifier = conversation(client)
        response = client.post(f'/api/conversations/{identifier}/messages', json={'text': 'Solicita la acción'})
        assert response.status_code == 200 and response.json()['approval']
        assert key not in response.text
        assert key not in app.state.database.get_conversation(identifier, 'owner')['pending']
