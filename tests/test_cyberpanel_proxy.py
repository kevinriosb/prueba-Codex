import re
import stat
from types import SimpleNamespace

import pytest

from scripts import configure_cyberpanel_proxy as proxy


DOMAIN = 'agente.example.test'
CONFIG = '''docRoot $VH_ROOT/public_html
vhDomain $VH_NAME
accesslog $VH_ROOT/logs/$VH_NAME.access_log {
  useServer 0
  logFormat "%h %l %u %t %r %>s %b"
  logHeaders 5
  rollingSize 10M
}
rewrite {
  enable 1
  autoLoadHtaccess 1
}
context /.well-known/acme-challenge {
  location /usr/local/lsws/Example/html/.well-known/acme-challenge
  allowBrowse 1
  rewrite {
    enable 0
  }
}
vhssl {
  keyFile /test/private.pem
  certFile /test/certificate.pem
}
'''


def test_proxy_preserves_acme_and_tls_and_omits_sensitive_access_logs():
    updated = proxy.render(CONFIG, DOMAIN)
    assert CONFIG[CONFIG.index('context /.well-known'):].rstrip() in updated
    assert 'handler                 lilyapp_agent' in updated
    assert 'https://' + DOMAIN + '%{REQUEST_URI}' in updated
    assert 'enablePrivateCache    0' in updated
    assert 'logHeaders              0' in updated
    accesslog = re.search(r'accesslog.*?\n}', updated, re.S).group()
    assert '%r' not in accesslog and '%q' not in accesslog and '%U' in accesslog
    assert proxy.render(updated, DOMAIN) == updated


@pytest.mark.parametrize('context', ['context / {\n}', 'context "/" {\n}', "context '/' {\n}"])
def test_existing_proxy_is_not_replaced(context):
    with pytest.raises(proxy.ConfigurationError, match='contexto raíz'):
        proxy.render(CONFIG + context, DOMAIN)


def test_modified_managed_block_is_not_overwritten():
    updated = proxy.render(CONFIG, DOMAIN).replace('127.0.0.1:18000', '127.0.0.1:9999')
    with pytest.raises(proxy.ConfigurationError, match='modificado'):
        proxy.render(updated, DOMAIN)


@pytest.mark.parametrize('rules', ['  enable 0\n', '  enable 1\n  rules <<<CUSTOM\nRewriteRule ^ /otra-app [L]\nCUSTOM\n'])
def test_custom_or_disabled_rewrite_is_not_replaced(rules):
    customized = CONFIG.replace('  enable 1\n  autoLoadHtaccess 1\n', rules)
    with pytest.raises(proxy.ConfigurationError, match='reescritura propias'):
        proxy.render(customized, DOMAIN)


def test_apply_restores_file_before_reload_when_validation_fails(tmp_path):
    path = tmp_path / 'vhost.conf'
    path.write_text(CONFIG)
    path.chmod(0o640)
    validators, reloads = [], []

    def validate():
        validators.append(path.read_text())
        if len(validators) == 1:
            raise proxy.ConfigurationError('Sintaxis no válida')

    with pytest.raises(proxy.ConfigurationError, match='restauró'):
        proxy.apply(path, CONFIG, proxy.render(CONFIG, DOMAIN), DOMAIN, validate,
                    lambda: reloads.append(True), lambda *args: None)
    assert path.read_text() == CONFIG and len(validators) == 2 and reloads == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    backups = list(tmp_path.glob('*.bak'))
    assert len(backups) == 1 and backups[0].read_text() == CONFIG
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_failed_public_health_restores_file_and_active_configuration(tmp_path, monkeypatch):
    path = tmp_path / 'vhost.conf'
    path.write_text(CONFIG)
    reloads = []
    monkeypatch.setattr(proxy.time, 'sleep', lambda _: None)

    def probe(*args):
        raise OSError('Sin conexión')

    with pytest.raises(proxy.ConfigurationError, match='restauró'):
        proxy.apply(path, CONFIG, proxy.render(CONFIG, DOMAIN), DOMAIN, lambda: None,
                    lambda: reloads.append(path.read_text()), probe)
    assert len(reloads) == 2 and reloads[-1] == CONFIG and path.read_text() == CONFIG


def test_successful_apply_keeps_original_backup_and_uses_expected_health(tmp_path):
    path = tmp_path / 'vhost.conf'
    path.write_text(CONFIG)
    updated = proxy.render(CONFIG, DOMAIN)
    probes = []
    backup = proxy.apply(path, CONFIG, updated, DOMAIN, lambda: None, lambda: None,
                         lambda *args: probes.append(args))
    assert backup.read_text() == CONFIG and path.read_text() == updated
    assert probes == [('https://' + DOMAIN + '/health', DOMAIN)]


def test_concurrent_edits_are_not_overwritten(tmp_path):
    path = tmp_path / 'vhost.conf'
    changed = CONFIG + '\n# Edición de otro administrador\n'
    path.write_text(changed)
    with pytest.raises(proxy.ConfigurationError, match='cambió'):
        proxy.apply(path, CONFIG, proxy.render(CONFIG, DOMAIN), DOMAIN, lambda: None,
                    lambda: None, lambda *args: None)
    assert path.read_text() == changed and list(tmp_path.glob('*.bak')) == []


WARNING = '2026-10-25 21:04:27.048393 [WARN] [123] [module] Un aviso ficticio con dato-privado-de-prueba\n'
ERROR = '2026-10-25 21:04:27.048394 [ERROR] [123] No se puede cargar la configuración\n'


def test_validation_accepts_native_warning_status_without_echoing_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr(proxy.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=WARNING, stderr=''))
    proxy.checked_command(['lshttpd', '-t'], allow_warnings=True)
    output = capsys.readouterr().out
    assert 'avisos: 1' in output and 'dato-privado-de-prueba' not in output


def test_reload_does_not_accept_nonzero_warning_status(monkeypatch):
    monkeypatch.setattr(proxy.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=WARNING, stderr=''))
    with pytest.raises(proxy.ConfigurationError, match='recarga'):
        proxy.checked_command(['lswsctrl', 'reload'])


@pytest.mark.parametrize('code,output', [(2,WARNING),(1,WARNING+ERROR),(1,WARNING+'Segmentation fault'),
                                       (0,ERROR),(1,''),(-11,WARNING)])
def test_warning_handling_still_rejects_errors_and_unrecognized_failures(monkeypatch, code, output):
    monkeypatch.setattr(proxy.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=code, stdout='', stderr=output))
    with pytest.raises(proxy.ConfigurationError, match='validación'):
        proxy.checked_command(['lshttpd', '-t'], allow_warnings=True)
