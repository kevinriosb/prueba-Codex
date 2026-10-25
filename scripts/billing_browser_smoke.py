"""Flujo fiscal en Chromium con documentos y servicios ficticios; nunca usa OpenAI/Gmail/SAT."""
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.billing_fakes import image
from tests.conftest import TEST_PASSWORD


def run():
    output=ROOT/'.local'/'billing-screenshots'
    output.mkdir(parents=True,exist_ok=True)
    with (output/'server.log').open('w') as log:
        process=subprocess.Popen([sys.executable,'-m','tests.serve_billing'],cwd=ROOT,stdout=log,stderr=log)
        try:
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError('No inició el servidor fiscal de pruebas.')
                try:
                    with urlopen('http://127.0.0.1:8767/health',timeout=1) as response:
                        if response.status==200:
                            break
                except (URLError,OSError):
                    time.sleep(0.1)
            else:
                raise RuntimeError('El servidor no respondió.')
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'))
                try:
                    context=browser.new_context(**playwright.devices['iPad (gen 7)'])
                    page=context.new_page()
                    errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.goto('http://127.0.0.1:8767')
                    page.locator('#username').fill('admin')
                    page.locator('#password').fill(TEST_PASSWORD)
                    page.locator('#login-button').click()
                    page.locator('#workspace').wait_for(state='visible')
                    page.locator('#open-billing').tap()
                    page.locator('#billing-content').wait_for(state='visible')
                    assert page.locator('#analyze-ticket').is_disabled()
                    page.locator('#csf-file').set_input_files({'name':'constancia-ficticia.png','mimeType':'image/png','buffer':image()})
                    page.locator('#csf-consent').check()
                    page.get_by_role('button',name='Extraer datos de la constancia',exact=True).click()
                    page.locator('#fiscal-draft').wait_for(state='visible')
                    assert page.locator('#draft-cfdi-use').input_value()==''
                    page.locator('#draft-cfdi-use').select_option('G03')
                    page.locator('#draft-email').fill('cliente@example.com')
                    page.get_by_role('button',name='Confirmar y guardar perfil',exact=True).click()
                    page.locator('#fiscal-profiles').get_by_text('EMPRESA DEMO SA DE CV',exact=True).wait_for()
                    selected=page.locator('#ticket-profile option').nth(1).get_attribute('value')
                    page.locator('#ticket-profile').select_option(selected)
                    page.locator('#ticket-file').set_input_files({'name':'ticket-ficticio.png','mimeType':'image/png','buffer':image('red')})
                    page.locator('#ticket-consent').check()
                    page.locator('#analyze-ticket').click()
                    page.get_by_role('button',name='Confirmo el destinatario para facturación',exact=True).wait_for()
                    page.get_by_role('button',name='Confirmo el destinatario para facturación',exact=True).click()
                    page.get_by_role('button',name='Preparar correo para revisión',exact=True).click()
                    page.locator('#billing-email-preview').wait_for(state='visible')
                    assert 'facturacion@example.com' in page.locator('#billing-email-preview').inner_text()
                    assert 'Constancia de Situación Fiscal' not in page.locator('#billing-email-preview').inner_text()
                    page.get_by_role('button',name='Rechazar envío',exact=True).click()
                    page.get_by_role('button',name='Preparar correo para revisión',exact=True).click()
                    page.get_by_role('button',name='Confirmar y enviar este correo',exact=True).click()
                    page.locator('#billing-ticket-detail').get_by_text('esperando respuesta',exact=True).wait_for()
                    page.get_by_role('button',name='Consultar respuestas en Gmail',exact=True).click()
                    page.locator('.billing-response').wait_for()
                    page.locator('.billing-response summary').click()
                    assert '<img' in page.locator('.billing-response pre').inner_text()
                    assert page.locator('#billing-ticket-detail img').count()==0
                    assert page.evaluate('window.fiscalXss === undefined')
                    page.screenshot(path=str(output/'fiscal-ipad.png'),animations='disabled')
                    page.locator('#close-billing').click()
                    page.reload()
                    page.locator('#open-billing').click()
                    page.locator('#fiscal-profiles').get_by_text('EMPRESA DEMO SA DE CV',exact=True).wait_for()
                    for width,height in [(1024,768),(768,1024),(375,812),(320,640)]:
                        page.set_viewport_size({'width':width,'height':height})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#close-billing').click()
                    page.locator('#menu-button').click()
                    page.locator('#logout').click()
                    page.locator('#login-view').wait_for(state='visible')
                    assert page.locator('#fiscal-profiles').inner_text()==''
                    assert not page.locator('#billing-dialog').is_visible()
                    assert errors==[],errors
                    print('PASS: CSF, consentimiento, uso explícito, perfil cifrado, ticket, correo verificado, revisión, rechazo/envío simulado, historial, controles iPad y logout.')
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__=='__main__':
    run()
