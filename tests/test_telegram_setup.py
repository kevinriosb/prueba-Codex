import base64
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from backend.app.config import Settings
from backend.app.errors import AppError
from scripts import configure_hostinger_telegram as setup
from scripts.configure_telegram import configure
from scripts.manage_telegram_users import change
from .conftest import TEST_PASSWORD


TOKEN = '123456789:' + 'fake_' * 8
SECRET = 's' * 43
KEY = base64.urlsafe_b64encode(b'k' * 32).decode()
ENV = (b'APP_LOGIN_PASSWORD=literal$#keep\nAI_API_KEY=keep-openai\n'
       b'BILLING_ENCRYPTION_KEY=' + KEY.encode() + b'\nTELEGRAM_BOT_TOKEN=\n'
       b'TELEGRAM_WEBHOOK_SECRET=\nTELEGRAM_ALLOWED_USER_IDS=\n')


def message(command, user=123, chat_type='private', is_bot=False):
    return {'message':{'text':command,'from':{'id':user,'is_bot':is_bot},
        'chat':{'id':user,'type':chat_type}}}


def test_setup_changes_only_telegram_variables_and_keeps_private_backup(tmp_path):
    path = tmp_path / '.env.hostinger'
    path.write_bytes(ENV)
    path.chmod(0o600)
    result = setup.render(ENV,TOKEN,123,SECRET)
    backup = setup.write_updated(path,result,ENV,label='telegram')
    assert backup.read_bytes() == ENV
    assert path.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    assert result.startswith(ENV.split(b'TELEGRAM_BOT_TOKEN=')[0])
    assert setup.variable(result,'TELEGRAM_BOT_TOKEN') == TOKEN
    assert setup.variable(result,'TELEGRAM_ALLOWED_USER_IDS') == '123'


@pytest.mark.parametrize('content', [ENV + b'telegram_bot_token=another\n', ENV.replace(b'TELEGRAM_BOT_TOKEN=',b'export TELEGRAM_BOT_TOKEN='), ENV.replace(b'TELEGRAM_ALLOWED_USER_IDS=\n',b'')])
def test_ambiguous_configuration_rejected(content):
    with pytest.raises(ValueError):
        setup.render(content,TOKEN,123,SECRET)


@pytest.mark.parametrize('token,user,secret', [('bad',123,SECRET),(TOKEN,True,SECRET),(TOKEN,-1,SECRET),(TOKEN,2**52,SECRET),(TOKEN,123,'short')])
def test_setup_validates_values_without_displaying_token(token,user,secret):
    with pytest.raises(ValueError) as caught:
        setup.render(ENV,token,user,secret)
    assert TOKEN not in str(caught.value)


def test_pairing_requires_exact_challenge_human_sender_and_matching_private_chat():
    command = '/vincular_nonce'
    updates = [message(command,user=666,chat_type='group'),message(command,user=777,is_bot=True),
               message(command+'otra'),message('/start'),message(command),message(command)]
    assert setup.paired_user(updates,command) == 123
    assert setup.paired_user([message('/vincular_otra')],command) is None
    with pytest.raises(ValueError,match='varias cuentas'):
        setup.paired_user([message(command),message(command,user=456)],command)


def test_api_only_reads_fixed_telegram_destination_and_redacts_failure():
    seen=[]

    class Offline:
        def open(self,request,*,timeout):
            seen.append(request)
            raise HTTPError(request.full_url,401,TOKEN,{},io.BytesIO(TOKEN.encode()))

    client=setup.BotAPI(TOKEN,opener=Offline())
    with pytest.raises(ValueError) as caught:
        client.call('getMe')
    assert TOKEN not in str(caught.value)
    assert seen[0].full_url == 'https://api.telegram.org/bot'+TOKEN+'/getMe'
    assert seen[0].data == b'{}'
    with pytest.raises(ValueError):
        client.call('setWebhook')
    assert len(seen)==1
    assert setup.NoRedirects().redirect_request(None,None,302,'',{},'https://other.invalid') is None


def test_interactive_enrollment_saves_only_verified_account(tmp_path,monkeypatch,capsys):
    path=tmp_path/'.env.hostinger'
    path.write_bytes(ENV)
    path.chmod(0o600)
    monkeypatch.setattr(setup,'ROOT',tmp_path)
    monkeypatch.setattr(setup.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(setup.sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(setup.getpass,'getpass',lambda prompt:TOKEN)
    answers=iter(['SI',''])
    monkeypatch.setattr('builtins.input',lambda prompt:next(answers))
    values=iter(['test_nonce',SECRET])
    monkeypatch.setattr(setup.secrets,'token_urlsafe',lambda count:next(values))

    class API:
        def __init__(self,token):
            assert token==TOKEN

        def call(self,method,payload=None):
            if method=='getMe':
                return {'is_bot':True,'username':'BotPruebaNuevoBot'}
            if method=='getWebhookInfo':
                return {'url':''}
            assert method=='getUpdates' and payload=={'limit':100,'timeout':0}
            return [message('/start',user=999),message('/vincular_test_nonce',user=123)]

    monkeypatch.setattr(setup,'BotAPI',API)
    assert setup.cli()==0
    output=capsys.readouterr()
    assert TOKEN not in output.out+output.err and SECRET not in output.out+output.err
    assert setup.variable(path.read_bytes(),'TELEGRAM_ALLOWED_USER_IDS')=='123'
    assert path.read_bytes().startswith(ENV.split(b'TELEGRAM_BOT_TOKEN=')[0])


def test_existing_configuration_is_preserved_before_requesting_secret(tmp_path,monkeypatch,capsys):
    path=tmp_path/'.env.hostinger'
    original=setup.render(ENV,TOKEN,123,SECRET)
    path.write_bytes(original)
    path.chmod(0o600)
    monkeypatch.setattr(setup,'ROOT',tmp_path)
    monkeypatch.setattr(setup.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(setup.sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(setup.getpass,'getpass',lambda prompt:pytest.fail('No debe pedir el token'))
    assert setup.cli()==1
    assert path.read_bytes()==original and list(tmp_path.iterdir())==[path]
    assert TOKEN not in capsys.readouterr().out


class WebhookClient:
    def __init__(self,previous='',current=None):
        self.previous,self.current=previous,current
        self.calls=[]
        self.closed=False

    async def _call(self,method,**kwargs):
        self.calls.append((method,kwargs))
        if method=='setWebhook':
            return True
        if len(self.calls)==1:
            return {'url':self.previous}
        return {'url':self.current or 'https://testserver/integrations/telegram/webhook'}

    async def close(self):
        self.closed=True


def settings(**overrides):
    return Settings(_env_file=None,app_login_password=TEST_PASSWORD,app_allowed_hosts='testserver',
        billing_encryption_key=KEY,telegram_bot_token=TOKEN,telegram_webhook_secret=SECRET,
        telegram_allowed_user_ids=overrides.pop('telegram_allowed_user_ids','123'),**overrides)


async def test_webhook_dry_run_does_not_call_external_service(capsys):
    client=WebhookClient()
    await configure(settings(),'https://testserver',client=client)
    assert client.calls==[] and TOKEN not in capsys.readouterr().out


async def test_webhook_application_preserves_pending_updates_and_verifies_target(capsys):
    client=WebhookClient()
    await configure(settings(),'https://testserver',apply=True,client=client)
    assert [name for name,_ in client.calls]==['getWebhookInfo','setWebhook','getWebhookInfo']
    payload=client.calls[1][1]['json']
    assert payload['secret_token']==SECRET and payload['drop_pending_updates'] is False
    assert payload['allowed_updates']==['message','callback_query']
    assert client.closed and TOKEN not in capsys.readouterr().out


async def test_another_services_webhook_is_never_replaced():
    client=WebhookClient(previous='https://other.invalid/'+TOKEN)
    with pytest.raises(AppError) as caught:
        await configure(settings(),'https://testserver',apply=True,client=client)
    assert caught.value.code=='telegram_webhook_exists' and TOKEN not in caught.value.message
    assert [name for name,_ in client.calls]==['getWebhookInfo'] and client.closed


@pytest.mark.parametrize('ids',['','123,','-123','123,abc','*',str(2**52)])
async def test_webhook_rejects_open_or_invalid_authorization_list(ids):
    client=WebhookClient()
    with pytest.raises(AppError):
        await configure(settings(telegram_allowed_user_ids=ids),'https://testserver',apply=True,client=client)
    assert client.calls==[]


def test_manual_invitation_and_revocation_only_change_authorization_list():
    original=setup.render(ENV,TOKEN,123,SECRET)
    invited,count=change(original,'456')
    assert count==2 and invited==original.replace(b'TELEGRAM_ALLOWED_USER_IDS=123\n',b'TELEGRAM_ALLOWED_USER_IDS=123,456\n')
    assert change(invited,'456')==(invited,2)
    revoked,count=change(invited,'456',remove=True)
    assert revoked==original and count==1
    with pytest.raises(ValueError):
        change(original,'123',remove=True)


@pytest.mark.parametrize('identifier',['@usuario','*','-123','123,456','000123',str(2**52)])
def test_manual_invitation_never_accepts_public_access_or_ambiguous_identifiers(identifier):
    with pytest.raises(ValueError):
        change(setup.render(ENV,TOKEN,123,SECRET),identifier)
