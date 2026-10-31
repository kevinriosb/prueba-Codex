"""Campos reales del portal: copiar solo valores extraídos, preguntar los ausentes."""
import pytest

from backend.app.billing.models import TicketExtraction
from .billing_fakes import TICKET,image
from .fake_model import ScriptedModel,call
from .test_billing import billing_client
from .test_portal_preparation import RealBrowser,ScopedExtractor,ROOT
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


@pytest.mark.parametrize('key,label,value',[
    ('web_id','Web ID','000AB-92'),('store_number','Tienda','00123'),
    ('register_number','Caja','004'),('toll_station','Caseta','NORTE 2'),
])
@pytest.mark.parametrize('readable',[True,False])
def test_extra_field_is_taken_only_when_printed_and_legible_otherwise_user_is_asked(billing_client,key,label,value,readable):
    class Extra(ScopedExtractor):
        async def extract_fields(self,mime,data,fields):
            self.calls.append(('fields',[{'key':f['key'],'label':f['label']} for f in fields]))
            return TicketExtraction(**(TICKET|{key:value if readable else None}))
    extractor=Extra();browser=RealBrowser()
    browser.pages.pages={ROOT:'<form><label>RFC<input required></label><label>'+label+'<input required></label></form>'}
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,ScriptedModel([[call('portal_browser_observe')]]),extractor=extractor,browser=browser)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    item=app.state.billing.store.list('ticket',owner(app))[0]
    assert extractor.calls[-1][0]=='fields' and {field['key'] for field in extractor.calls[-1][1]}=={key}
    assert item.get(key)==(value if readable else None)
    if readable:
        assert label+': '+value in telegram.messages[-1][1]
    else:
        assert label in telegram.messages[-1][1]
        assert any(data=='td:'+item['id']+':'+key for _,data in telegram.messages[-1][2])
        before=len(extractor.calls)
        chat(data='td:'+item['id']+':'+key);chat(value)
        assert app.state.billing.resolve_ticket(owner(app),item['id'])[key]==value
        assert len(extractor.calls)==before
    assert 'fill' not in browser.calls and mail.sent==sat.calls==[]
