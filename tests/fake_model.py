import asyncio
import json
import uuid

from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


def answer(text):
    return ResponseOutputMessage(id='msg_' + uuid.uuid4().hex, type='message', role='assistant', status='completed',
        content=[ResponseOutputText(type='output_text', text=text, annotations=[])])


def call(name, **arguments):
    return ResponseFunctionToolCall(id='fc_' + uuid.uuid4().hex, type='function_call', call_id='call_' + uuid.uuid4().hex,
        name=name, arguments=json.dumps(arguments), status='completed')


class ScriptedModel(Model):
    """Doble de prueba. Runner y las herramientas son los reales del Agents SDK."""
    def __init__(self, outputs=None, *, failure=None, pause=0):
        self.outputs = list(outputs or [[answer('Respuesta local de prueba.')]])
        self.inputs = []
        self.failure, self.pause = failure, pause

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs):
        self.inputs.append(input)
        if self.pause:
            await asyncio.sleep(self.pause)
        if self.failure:
            raise self.failure
        output = self.outputs.pop(0) if self.outputs else [answer('Respuesta local de prueba.')]
        return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=40, output_tokens=20, total_tokens=60), response_id='resp_' + uuid.uuid4().hex)

    async def stream_response(self, *args, **kwargs):
        if False:
            yield None
        raise NotImplementedError('Las pruebas no utilizan streaming.')
