"""Prueba funcional local con Chromium y modelo simulado. No usa la API de OpenAI."""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.conftest import TEST_PASSWORD


def run():
    output = ROOT / '.local' / 'screenshots'
    output.mkdir(parents=True, exist_ok=True)
    log = (ROOT / '.local' / 'ui-test-server.log').open('w')
    process = subprocess.Popen([sys.executable, '-m', 'tests.serve_ui'], cwd=ROOT, stdout=log, stderr=log)
    try:
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError('El servidor de prueba no inició; consulta .local/ui-test-server.log')
            try:
                with urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(.1)
        else:
            raise RuntimeError('El servidor de prueba no respondió')
        with sync_playwright() as playwright:
            executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH')
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                page = browser.new_page(viewport={'width': 1180, 'height': 820}, timezone_id='America/Mexico_City')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8766')
                page.locator('#login-view').wait_for(state='visible')
                page.locator('#username').fill('admin')
                page.locator('#password').fill(TEST_PASSWORD)
                page.locator('#login-button').click()
                page.locator('#welcome').wait_for(state='visible')
                page.screenshot(path=str(output / 'welcome-desktop.png'))
                page.get_by_role('button', name='Hacer un cálculo', exact=False).click()
                page.locator('#send-button').click()
                page.get_by_text('Cada persona paga 287.50 pesos.', exact=False).wait_for()
                assert page.locator('.tool-event').count() == 1
                page.reload()
                page.get_by_text('Cada persona paga 287.50 pesos.', exact=False).wait_for()
                for name, width, height in [('ipad-landscape', 1024, 768), ('ipad-portrait', 768, 1024), ('mobile', 375, 812), ('small-mobile', 320, 640)]:
                    page.set_viewport_size({'width': width, 'height': height})
                    page.screenshot(path=str(output / f'{name}.png'), animations='disabled')
                    if width <= 900:
                        bounds = page.locator('#sidebar').bounding_box()
                        assert bounds['x'] + bounds['width'] <= 1, 'El menú debe estar cerrado en vertical'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name
                    assert page.locator('#send-button').is_visible()
                    print(f'PASS: {name}, sin desbordamiento lateral.')
                page.set_viewport_size({'width': 1024, 'height': 768})
                page.locator('#message-input').fill('Una acción de prueba')
                page.locator('#send-button').click()
                page.locator('#approval-panel').wait_for(state='visible')
                assert page.locator('#message-input').is_disabled()
                page.locator('#reject-action').click()
                page.get_by_text('La acción fue rechazada y no se ejecutó.', exact=True).wait_for()
                page.locator('#message-input').fill('Otra acción de prueba')
                page.locator('#send-button').click()
                page.locator('#approval-panel').wait_for(state='visible')
                page.locator('#approve-action').click()
                page.get_by_text('Resultado de la herramienta:', exact=False).last.wait_for()
                page.locator('#message-input').fill('Muestra html de prueba')
                page.locator('#send-button').click()
                page.get_by_text('Texto literal, sin ejecutar.', exact=False).wait_for()
                assert page.evaluate('window.xssDetected === undefined')
                assert page.locator('#messages img').count() == 0
                page.locator('#message-input').fill('Provoca un fallo')
                page.locator('#send-button').click()
                page.locator('#chat-error').wait_for(state='visible')
                assert 'ERROR_PRIVADO' not in page.locator('body').inner_text()
                page.locator('#logout').click()
                page.locator('#login-view').wait_for(state='visible')
                assert not page.locator('#workspace').is_visible()
                assert errors == [], errors
                print('PASS: login, cálculo con SDK, historial tras recargar, aprobar/rechazar, salida HTML inerte, errores seguros y logout.')
                tablet = browser.new_context(**playwright.devices['iPad (gen 7)'], timezone_id='America/Mexico_City')
                touch = tablet.new_page()
                touch.goto('http://127.0.0.1:8766')
                touch.locator('#username').fill('admin')
                touch.locator('#password').fill(TEST_PASSWORD)
                touch.locator('#login-button').tap()
                touch.locator('#workspace').wait_for(state='visible')
                touch.locator('#menu-button').tap()
                touch.locator('#new-chat').tap()
                touch.locator('#welcome').wait_for(state='visible')
                touch.get_by_role('button', name='Hacer un cálculo', exact=False).tap()
                touch.locator('#send-button').tap()
                touch.get_by_text('Cada persona paga 287.50 pesos.', exact=False).wait_for()
                tablet.close()
                print('PASS: controles táctiles y menú de conversaciones en emulación de iPad.')
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


if __name__ == '__main__':
    run()
