import io
import json
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import build_opener

import pytest

from scripts import check_openai as diagnostic


SECRET = 'credential-only-for-offline-tests'


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.code = status


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def open(self, request, *, timeout):
        assert timeout == 15
        self.requests.append(request)
        status, body = next(self.replies)
        if status >= 400:
            raise HTTPError(request.full_url, status, 'private-provider-message-' + SECRET,
                            {}, Response(status, body))
        return Response(status, body)


def run(replies, **overrides):
    opener = Opener(replies)
    output = []
    code = diagnostic.check(dict(AI_API_KEY=SECRET, AI_MODEL='gpt-4.1-mini') | overrides,
                            opener=opener, emit=output.append)
    text = '\n'.join(output)
    assert SECRET not in text
    assert 'private-provider-message' not in text
    return code, text, opener.requests


UNAUTHENTICATED = (401, {'error': {'code': 'invalid_api_key'}})
ACCEPTED = (400, {'error': {'code': 'missing_required_parameter', 'param': 'model'}})
MODEL = (200, {'id': 'gpt-4.1-mini', 'object': 'model'})


def test_effective_container_environment_without_generation_or_prompt():
    code, text, requests = run([UNAUTHENTICATED, ACCEPTED, MODEL])
    assert code == 0 and 'NO verifica saldo' in text
    assert [request.method for request in requests] == ['POST', 'POST', 'GET']
    assert [request.full_url for request in requests] == [
        'https://api.openai.com/v1/responses', 'https://api.openai.com/v1/responses',
        'https://api.openai.com/v1/models/gpt-4.1-mini']
    assert requests[0].get_header('Authorization') is None
    assert requests[1].get_header('Authorization') == 'Bearer ' + SECRET
    assert requests[0].data == requests[1].data == b'{}'
    assert requests[2].data is None


@pytest.mark.parametrize('token', ['', ' ' + SECRET, SECRET + '\n', SECRET + '\x00',
                                  '"' + SECRET + '"', 'Bearer ' + SECRET,
                                  'AI_API_KEY=' + SECRET, 'clé-privée'])
def test_invalid_or_missing_key_never_makes_requests(token):
    code, text, requests = run([], AI_API_KEY=token)
    assert code == 1 and 'Configuración:' in text and not requests


@pytest.mark.parametrize('status', [200, 400, 403, 502])
def test_permissive_or_unavailable_unauthenticated_endpoint_is_not_key_validation(status):
    code, text, requests = run([(status, {'message': SECRET})])
    assert code == 1 and 'No se envió la clave' in text and len(requests) == 1


@pytest.mark.parametrize('status,code,expected', [
    (401, 'invalid_api_key', 'Credencial rechazada'),
    (403, None, 'Acceso denegado'),
    (429, 'insufficient_quota', 'saldo de la API insuficientes'),
    (429, 'rate_limit_exceeded', 'limitado las solicitudes'),
    (503, None, 'error de servicio'),
    (400, None, 'Respuesta inesperada'),
])
def test_http_errors_report_only_safe_categories(status, code, expected):
    result, text, requests = run([UNAUTHENTICATED, (status, {'error': {
        'code': code, 'message': SECRET, 'param': SECRET}})])
    assert result == 1 and expected in text and len(requests) == 2


def test_success_status_or_wrong_validation_error_does_not_prove_authentication():
    for reply in [(200, {'message': SECRET}),
                  (400, {'error': {'code': 'missing_required_parameter', 'param': 'input'}})]:
        code, text, requests = run([UNAUTHENTICATED, reply])
        assert code == 1 and 'Respuesta inesperada' in text and len(requests) == 2


def test_restricted_model_listing_is_not_interpreted_as_generation_failure():
    code, text, requests = run([UNAUTHENTICATED, ACCEPTED, (403, {'error': {'message': SECRET}})])
    assert code == 1 and 'no demuestra que generar esté bloqueado' in text


def test_unavailable_model_and_invalid_success_body():
    for reply in [(404, {'error': {'message': SECRET}}), (200, {'id': SECRET, 'object': 'model'})]:
        code, text, requests = run([UNAUTHENTICATED, ACCEPTED, reply])
        assert code == 1 and len(requests) == 3


def test_malformed_and_oversized_response_does_not_echo_raw_body():
    for body in [b'<html>' + SECRET.encode(), b'x' * (diagnostic.MAX_BODY + 1), [SECRET]]:
        code, text, requests = run([UNAUTHENTICATED, (400, body)])
        assert code == 1 and 'Respuesta inesperada' in text


@pytest.mark.parametrize('reason,expected', [
    (ssl.SSLCertVerificationError(1, SECRET), 'certificado TLS'),
    (socket.gaierror(-2, SECRET), 'resolver el nombre'),
    (TimeoutError(SECRET), 'tiempo de espera'),
    (ConnectionError(SECRET), 'consulta HTTPS'),
])
def test_connection_diagnostics_never_render_exception(reason, expected):
    class Offline:
        def open(self, request, *, timeout):
            raise URLError(reason)

    output = []
    assert diagnostic.check({'AI_API_KEY': SECRET}, opener=Offline(), emit=output.append) == 1
    assert expected in '\n'.join(output) and SECRET not in '\n'.join(output)


def test_redirect_is_disabled_before_forwarding_authorization():
    opener = build_opener(diagnostic.NoRedirects())
    handler = next(handler for handler in opener.handlers if isinstance(handler, diagnostic.NoRedirects))
    assert handler.redirect_request(None, None, 302, SECRET, {}, 'https://other.invalid') is None
    code, text, requests = run([UNAUTHENTICATED, (302, {'message': SECRET})])
    assert code == 1 and 'Redirección rechazada' in text


def test_custom_base_url_is_flagged_but_never_used():
    code, text, requests = run([UNAUTHENTICATED, ACCEPTED, MODEL], OPENAI_BASE_URL='https://other.invalid/' + SECRET)
    assert code == 0 and 'existe OPENAI_BASE_URL' in text
    assert all(request.full_url.startswith(diagnostic.BASE_URL) for request in requests)


def test_unexpected_error_entrypoint_has_no_traceback_or_secrets(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(diagnostic, 'check', fail)
    assert diagnostic.main() == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err and not captured.err
