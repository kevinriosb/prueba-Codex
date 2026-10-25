import getpass
import os
from pathlib import Path
import pty
import select
import stat
import subprocess
import sys
import termios
import time
import warnings

import pytest

from scripts import update_openai_key as updater


# Valor ficticio: no tiene el formato completo ni permite acceder a un servicio.
TOKEN = 'sk-' + 'demo_' * 8
ORIGINAL = (b'# configuracion privada\r\nAPP_LOGIN_PASSWORD=literal$#\"original\r\n'
            b'AI_API_KEY=old-demo\r\nAI_MODEL=gpt-4.1-mini\r\n'
            b'BILLING_ENCRYPTION_KEY=conservar-exactamente\r\n'
            b'TELEGRAM_BOT_TOKEN=no-tocar\r\nGMAIL_CLIENT_SECRET=sin-cambios')


def configuration(tmp_path, content=ORIGINAL):
    path = tmp_path / '.env.hostinger'
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_only_key_changes_with_private_backup_and_exact_preservation(tmp_path):
    path = configuration(tmp_path)
    metadata = path.stat()
    unrelated = tmp_path / 'datos.sqlite3'
    unrelated.write_bytes(b'database-unchanged')
    backup = updater.update_key(path, TOKEN, ORIGINAL)
    expected = ORIGINAL.replace(b'AI_API_KEY=old-demo\r\n', b'AI_API_KEY=' + TOKEN.encode() + b'\r\n')
    assert path.read_bytes() == expected
    assert backup.read_bytes() == ORIGINAL
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert (path.stat().st_uid, path.stat().st_gid) == (metadata.st_uid, metadata.st_gid)
    assert unrelated.read_bytes() == b'database-unchanged'
    assert set(tmp_path.iterdir()) == {path, backup, unrelated}


@pytest.mark.parametrize('ending', [b'\n', b'\r\n', b''])
def test_original_line_endings_and_comments_are_preserved(ending):
    original = b'# AI_API_KEY=example\nAI_API_KEY=old' + ending
    assert updater.replace_value(original, TOKEN) == b'# AI_API_KEY=example\nAI_API_KEY=' + TOKEN.encode() + ending


@pytest.mark.parametrize('content', [
    b'APP_LOGIN_PASSWORD=original\n',
    b'AI_API_KEY=a\nAI_API_KEY=b\n',
    b'AI_API_KEY=a\nai_api_key=b\n',
    b'export AI_API_KEY=a\n',
    b'AI_API_KEY =a\n',
    b'AI_API_KEY\n',
])
def test_ambiguous_configuration_does_not_create_backup_or_change_file(tmp_path, content):
    path = configuration(tmp_path, content)
    with pytest.raises(ValueError):
        updater.update_key(path, TOKEN, content)
    assert path.read_bytes() == content and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('token', ['', ' ' + TOKEN, TOKEN + '\n', '"' + TOKEN + '"',
                                  'Bearer ' + TOKEN, 'AI_API_KEY=' + TOKEN, 'not-an-openai-key'])
def test_bad_input_never_enters_error_or_configuration(tmp_path, token):
    path = configuration(tmp_path)
    with pytest.raises(ValueError) as caught:
        updater.update_key(path, token, ORIGINAL)
    assert TOKEN not in str(caught.value)
    assert path.read_bytes() == ORIGINAL and list(tmp_path.iterdir()) == [path]


def test_identical_value_does_not_rewrite_or_back_up(tmp_path):
    content = updater.replace_value(ORIGINAL, TOKEN)
    path = configuration(tmp_path, content)
    metadata = path.stat()
    assert updater.update_key(path, TOKEN, content) is None
    assert path.stat().st_ino == metadata.st_ino and list(tmp_path.iterdir()) == [path]


def test_symlink_and_public_configuration_are_rejected(tmp_path):
    path = configuration(tmp_path)
    link = tmp_path / 'linked'
    link.symlink_to(path)
    with pytest.raises(OSError):
        updater.update_key(link, TOKEN, ORIGINAL)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='permisos privados'):
        updater.update_key(path, TOKEN, ORIGINAL)
    assert path.read_bytes() == ORIGINAL


def test_changed_file_before_prompt_completion_is_not_replaced(tmp_path):
    path = configuration(tmp_path, ORIGINAL + b'\nANOTHER=edit\n')
    with pytest.raises(ValueError, match='cambió mientras'):
        updater.update_key(path, TOKEN, ORIGINAL)
    assert path.read_bytes().endswith(b'ANOTHER=edit\n') and list(tmp_path.iterdir()) == [path]


def test_changed_file_while_backing_up_is_not_replaced(tmp_path, monkeypatch):
    path = configuration(tmp_path)
    actual_read = updater.read_private
    reads = []

    def racing_read(candidate):
        reads.append(candidate)
        if len(reads) == 2:
            path.write_bytes(ORIGINAL + b'\nANOTHER=edit\n')
        return actual_read(candidate)

    monkeypatch.setattr(updater, 'read_private', racing_read)
    with pytest.raises(ValueError, match='edición más reciente'):
        updater.update_key(path, TOKEN, ORIGINAL)
    assert path.read_bytes().endswith(b'ANOTHER=edit\n')
    backups = list(tmp_path.glob('.env.hostinger.openai-*.bak'))
    assert len(backups) == 1 and backups[0].read_bytes() == ORIGINAL
    assert set(tmp_path.iterdir()) == {path, backups[0]}


def test_failed_atomic_replace_keeps_original_and_private_backup(tmp_path, monkeypatch):
    path = configuration(tmp_path)

    def fail(*args):
        raise OSError('simulated-write-failure')

    monkeypatch.setattr(updater.os, 'replace', fail)
    with pytest.raises(OSError):
        updater.update_key(path, TOKEN, ORIGINAL)
    backups = list(tmp_path.glob('.env.hostinger.openai-*.bak'))
    assert path.read_bytes() == ORIGINAL and len(backups) == 1
    assert backups[0].read_bytes() == ORIGINAL and set(tmp_path.iterdir()) == {path, backups[0]}


def interactive(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, 'ROOT', tmp_path)
    monkeypatch.setattr(updater.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(updater.sys.stdout, 'isatty', lambda: True)


def test_interactive_entrypoint_never_echoes_new_or_previous_values(tmp_path, monkeypatch, capsys):
    configuration(tmp_path)
    interactive(monkeypatch, tmp_path)
    monkeypatch.setattr(updater.getpass, 'getpass', lambda prompt: TOKEN)
    assert updater.cli() == 0
    output = capsys.readouterr()
    assert 'Actualizada únicamente AI_API_KEY' in output.out
    assert TOKEN not in output.out + output.err and 'old-demo' not in output.out + output.err


def test_noninteractive_input_is_refused_before_prompt(monkeypatch, capsys):
    monkeypatch.setattr(updater.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(updater.getpass, 'getpass', lambda prompt: pytest.fail('No debe pedir secretos'))
    assert updater.cli() == 1 and 'Termius' in capsys.readouterr().out


def test_echo_fallback_is_blocked_and_empty_input_cancels(tmp_path, monkeypatch, capsys):
    path = configuration(tmp_path)
    interactive(monkeypatch, tmp_path)

    def warning(prompt):
        warnings.warn('no-hidden-input', getpass.GetPassWarning)
        pytest.fail('El aviso debe interrumpir antes de una entrada visible')

    monkeypatch.setattr(updater.getpass, 'getpass', warning)
    assert updater.cli() == 1 and 'ocultar' in capsys.readouterr().out
    monkeypatch.setattr(updater.getpass, 'getpass', lambda prompt: '')
    assert updater.cli() == 1 and 'Cancelado' in capsys.readouterr().out
    assert path.read_bytes() == ORIGINAL and list(tmp_path.iterdir()) == [path]


def test_real_terminal_hides_pasted_key_and_preserves_other_configuration(tmp_path):
    path = configuration(tmp_path)
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    script = scripts / 'update_openai_key.py'
    script.write_bytes(Path(updater.__file__).read_bytes())
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=slave)
    output = b''
    try:
        deadline = time.monotonic() + 5
        while b'Enter cancela): ' not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                output += os.read(master, 4096)
        assert b'Enter cancela): ' in output
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, TOKEN.encode() + b'\n')
        assert process.wait(timeout=5) == 0
        while select.select([master], [], [], 0.1)[0]:
            output += os.read(master, 4096)
        assert TOKEN.encode() not in output and b'old-demo' not in output
        assert path.read_bytes() == updater.replace_value(ORIGINAL, TOKEN)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def test_unexpected_file_error_is_redacted(monkeypatch, capsys):
    def fail():
        raise OSError(TOKEN)

    monkeypatch.setattr(updater, 'main', fail)
    assert updater.cli() == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and not output.err
