"""Servidor aislado para pruebas de navegador. No es el arranque de producción."""
import json
import tempfile
from pathlib import Path

import uvicorn

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.tools.registry import ToolSpec
from .conftest import TEST_PASSWORD
from .fake_model import ScriptedModel, answer, call


class BrowserModel(ScriptedModel):
    async def get_response(self, system_instructions, input, *args, **kwargs):
        last = input[-1] if isinstance(input, list) else {'content': input}
        if last.get('type') == 'function_call_output':
            output = str(last.get('output', ''))
            if 'rechaz' in output.lower():
                self.outputs = [[answer('La acción fue rechazada y no se ejecutó.')]]
            elif '287.5' in output:
                self.outputs = [[answer('El total con propina es 575.00 pesos. Cada persona paga 287.50 pesos.')]]
            else:
                self.outputs = [[answer('Resultado de la herramienta: ' + output)]]
        else:
            content = str(last.get('content', '')).lower()
            if 'fallo' in content:
                raise RuntimeError('ERROR_PRIVADO_QUE_NO_DEBE_FILTRARSE')
            if 'html' in content:
                self.outputs = [[answer('<img src=x onerror="window.xssDetected=true"> Texto literal, sin ejecutar.')]]
            elif 'acción' in content:
                self.outputs = [[call('save_test_note', note='Nota local de prueba')]]
            elif 'hora' in content or 'fecha' in content:
                self.outputs = [[call('current_server_time')]]
            elif '500' in content or 'propina' in content:
                self.outputs = [[call('calculator', expression='500 * 1.15 / 2')]]
            else:
                self.outputs = [[answer('Respuesta de un modelo simulado para comprobar la interfaz.')]]
        return await super().get_response(system_instructions, input, *args, **kwargs)


async def save_test_note(note: str) -> str:
    """Devuelve una nota ficticia; no escribe ni conecta sistemas externos."""
    return 'Nota ficticia confirmada: ' + note


if __name__ == '__main__':
    Path('.local').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir='.local') as directory:
        settings = Settings(_env_file=None, app_login_password=TEST_PASSWORD,
            app_database_path=Path(directory) / 'browser.sqlite3', app_allowed_hosts='127.0.0.1,localhost', ai_model='Modelo simulado · prueba local')
        app = create_app(settings, model=BrowserModel(), extra_tools=(ToolSpec(save_test_note, 'write'),))
        uvicorn.run(app, host='127.0.0.1', port=8766, access_log=False)
