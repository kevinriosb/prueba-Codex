import stat
import sys

import pytest

from scripts import retire_cyberpanel_domain as retire


OLD = 'old.example.test'
KEEP = ['web.example.test', 'inicio.example.test', 'agente.example.test']


def virtual_host(name):
    return ('virtualHost ' + name + ' {\n  vhRoot /home/$VH_NAME\n'
            '  configFile $SERVER_ROOT/conf/vhosts/$VH_NAME/vhost.conf\n  restrained 1\n}\n')


def configuration():
    legacy = [OLD, 'mail.' + OLD, 'n8n.' + OLD, 'n8n.' + OLD, 'strapi.' + OLD]
    text = ''.join(virtual_host(name) for name in legacy + KEEP + ['server.example.test'])
    for name, address, secure, targets, cert_domain in [
        ('Default','*:80',False,[OLD,'mail.'+OLD,'n8n.'+OLD,'strapi.'+OLD],'server.example.test'),
        ('SSL','*:443',True,[OLD,'mail.'+OLD,'n8n.'+OLD,'strapi.'+OLD],'server.example.test'),
        ('SSL IPv6','[ANY]:443',True,[OLD,'mail.'+OLD,'strapi.'+OLD],OLD),
    ]:
        text += 'listener ' + name + ' {\n  address ' + address + '\n  secure ' + str(int(secure)) + '\n'
        if secure:
            text += '  keyFile /etc/letsencrypt/live/' + cert_domain + '/privkey.pem\n'
            text += '  certFile /etc/letsencrypt/live/' + cert_domain + '/fullchain.pem\n'
        text += ''.join('  map ' + target + ' ' + target + '\n' for target in targets + KEEP + ['server.example.test'])
        text += '}\n'
    return text + 'module cyberpanel_ols {\n  ls_enabled 1\n}\n'


def test_retirement_preserves_active_sites_and_reuses_existing_tls_fallback():
    original = configuration()
    updated, plan = retire.render(original, OLD, KEEP)
    assert len(plan['virtual_hosts']) == 5 and plan['mappings'] == 11
    assert plan['virtual_hosts'].count('n8n.' + OLD) == 2
    assert plan['certificates'] == [('SSL IPv6','/etc/letsencrypt/live/server.example.test/privkey.pem',
                                  '/etc/letsencrypt/live/server.example.test/fullchain.pem')]
    assert OLD not in updated
    for host in KEEP:
        assert virtual_host(host) in updated
        assert updated.count('  map ' + host + ' ' + host + '\n') == 3
    assert '  address [ANY]:443\n' in updated and '  address *:443\n' in updated
    assert updated.endswith('module cyberpanel_ols {\n  ls_enabled 1\n}\n')
    repeated, next_plan = retire.render(updated, OLD, KEEP)
    assert repeated == updated and next_plan == {'virtual_hosts':[],'mappings':0,'certificates':[]}


@pytest.mark.parametrize('mapping', ['  map '+OLD+' '+OLD+',web.example.test\n',
                                   '  map web.example.test web.example.test,'+OLD+'\n'])
def test_mixed_active_aliases_block_retirement(mapping):
    original = configuration().replace('  map '+OLD+' '+OLD+'\n', mapping, 1)
    with pytest.raises(retire.ConfigurationError, match='otros dominios|mezclados'):
        retire.render(original, OLD, KEEP)


def test_ambiguous_backup_certificate_is_not_selected():
    original = configuration() + '''listener OtherSSL {
  address *:444
  secure 1
  keyFile /another/key.pem
  certFile /another/cert.pem
  map web.example.test web.example.test
}
'''
    with pytest.raises(retire.ConfigurationError, match='inequívoco'):
        retire.render(original, OLD, KEEP)


@pytest.mark.parametrize('extra', ['}\n','listener Incomplete {\n'])
def test_unrecognized_global_structure_is_rejected(extra):
    with pytest.raises(retire.ConfigurationError, match='llaves|incompletos'):
        retire.render(configuration()+extra, OLD, KEEP)


def test_protected_host_cannot_be_retired_or_missing():
    with pytest.raises(retire.ConfigurationError, match='separados'):
        retire.render(configuration(), OLD, KEEP+[OLD])
    with pytest.raises(retire.ConfigurationError, match='No se encontró'):
        retire.render(configuration(), OLD, ['missing.example.test'])


def test_staging_preserves_files_database_and_private_configuration_backup(tmp_path):
    path = tmp_path / 'httpd_config.conf'
    original = configuration()
    path.write_text(original)
    path.chmod(0o640)
    data = tmp_path / 'n8n.sqlite'
    data.write_bytes(b'datos ficticios de otra aplicacion')
    old_file = tmp_path / 'retired-vhost.conf'
    old_file.write_text('configuracion antigua que se conserva')
    updated, _ = retire.render(original, OLD, KEEP)
    checks = []
    backup = retire.stage(path, original, updated, lambda: checks.append(path.read_text()))
    assert checks == [updated] and path.read_text() == updated and backup.read_text() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert data.read_bytes() == b'datos ficticios de otra aplicacion'
    assert old_file.read_text() == 'configuracion antigua que se conserva'


def test_validation_failure_restores_even_previously_invalid_global_file(tmp_path):
    path = tmp_path / 'httpd_config.conf'
    original = configuration()
    path.write_text(original)
    updated, _ = retire.render(original, OLD, KEEP)

    def fail():
        assert path.read_text() == updated
        raise retire.ConfigurationError('Otro error global')

    with pytest.raises(retire.ConfigurationError, match='restauró.*no se recargó'):
        retire.stage(path, original, updated, fail)
    assert path.read_text() == original
    assert len(list(tmp_path.glob('*.bak'))) == 1


def test_concurrent_change_does_not_overwrite_another_edit(tmp_path):
    path = tmp_path / 'httpd_config.conf'
    original = configuration()
    modified = original+'# otra edicion\n'
    path.write_text(modified)
    updated, _ = retire.render(original, OLD, KEEP)
    with pytest.raises(retire.ConfigurationError, match='cambió'):
        retire.stage(path, original, updated, lambda: None)
    assert path.read_text() == modified and list(tmp_path.glob('*.bak')) == []


def test_default_command_is_read_only(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'httpd_config.conf'
    original = configuration()
    path.write_text(original)
    monkeypatch.setattr(retire, 'SERVER_CONFIG', path)
    monkeypatch.setattr(sys, 'argv', ['retire', '--retire-domain', OLD, '--keep-hosts', ','.join(KEEP)])
    monkeypatch.setattr(retire, 'checked_command', lambda *args, **kwargs: pytest.fail('No se debe recargar ni validar el archivo anterior'))
    retire.main()
    assert path.read_text() == original and list(tmp_path.glob('*.bak')) == []
    assert 'Plan preparado sin cambios' in capsys.readouterr().out
