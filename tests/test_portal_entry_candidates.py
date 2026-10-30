"""Direcciones evaluadas por su frase; transporte simulado, sin comercios reales."""
import json
import socket

import httpx2
import pytest

from backend.app.billing.extraction import DocumentExtractor
from backend.app.billing.models import TicketExtraction,TicketRouting
from .billing_fakes import TICKET, image
from .fake_model import ScriptedModel, answer
from .test_billing import billing_client, ticket


HUB='https://hub.example.com.mx/'
BAD='https://branch.invalid/'


def addresses():
    return [
        {'url':HUB, 'evidence':'Tu comprobante obténlo en el portal: '+HUB, 'purpose':'billing'},
        {'url':BAD, 'evidence':'Datos Sucursal: '+BAD, 'purpose':'other'},
        {'url':'https://experience.example/', 'evidence':'Tu opinión: https://experience.example/', 'purpose':'other'},
    ]


def test_real_sdk_preserves_all_printed_addresses_and_chooses_by_their_own_phrase(billing_client):
    raw={key:value for key,value in TICKET.items() if key in TicketRouting.model_fields}
    raw.update(printed_addresses=addresses(),billing_url=BAD,billing_url_evidence=None)
    client,app,*_=billing_client(model=ScriptedModel([[answer(json.dumps(raw))]]))
    result=client.portal.call(DocumentExtractor(app.state.service,app.state.settings).extract_routing,'image/jpeg',image())
    assert result.billing_url==HUB
    assert [item.url for item in result.printed_addresses]==[HUB,BAD,'https://experience.example/']
    assert 'Datos Sucursal' in result.printed_addresses[1].evidence


def test_no_positive_printed_context_does_not_turn_a_branch_or_survey_into_a_portal():
    raw=TICKET | {'printed_addresses':addresses()[1:], 'billing_url':BAD}
    assert TicketExtraction.model_validate(raw).billing_url is None


def test_printed_portal_is_checked_before_awaiting_form_or_identified_text(billing_client):
    calls=[]
    async def probe(url):
        calls.append(url)
        return {'url':url,'reachable':True,'reason':None,'status':200}
    client,app,extractor,*_=billing_client(portal_probe=probe)
    extractor.ticket.update(billing_url=HUB,printed_addresses=addresses())
    result=ticket(client)
    assert calls==[HUB]
    assert result['portal_entry_check']['reachable'] is True
    assert result['billing_url']==HUB
    assert result['portal_rejected_addresses'][0]['url']==BAD
    assert result['portal_rejected_addresses'][0]['reason']=='printed_other'


def test_unreachable_printed_address_is_discarded_and_official_search_really_runs(billing_client):
    from .test_official_portal_discovery import Modern
    class Printed(Modern):
        async def extract_routing(self,mime,data):
            return TicketExtraction(**(self.ticket | {'billing_url':BAD,'billing_url_evidence':'Facturación '+BAD,
                'printed_addresses':[{'url':BAD,'evidence':'Facturación '+BAD,'purpose':'billing'}]}))
    extractor=Printed();extractor.found=HUB
    checked=[]
    async def probe(url):
        checked.append(url)
        return {'url':url,'reachable':url!=BAD,'reason':'dns_error' if url==BAD else None,'status':None if url==BAD else 200}
    from .test_telegram_agent import create, button, owner
    from .test_telegram_documents import confirm_profile
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,extractor=extractor,portal_probe=probe)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    saved=app.state.billing.store.list('ticket',owner(app))[0]
    assert checked==[BAD,HUB] and extractor.searches==[extractor.ticket['merchant']]
    assert saved['billing_url']==HUB and saved['portal_entry_check']['reachable']
    assert any(item['url']==BAD and item['reason']=='dns_error' for item in saved['portal_rejected_addresses'])
    assert all(BAD not in text for _,text,_ in telegram.messages)
    assert mail.sent==sat.calls==[]


async def test_probe_checks_dns_and_response_without_following_or_observing_a_redirect():
    from backend.app.billing.portal_probe import probe_url
    from backend.browser_worker.policy import PublicFetcher
    requests=[]
    async def resolver(host):
        if host=='branch.invalid':raise socket.gaierror('private internal details')
        return ['93.184.216.34']
    def respond(request):
        requests.append(request)
        return httpx2.Response(302,headers={'location':'https://unapproved.example/form'},text='PRIVATE-FORM-CONTENT')
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
    try:
        bad=await probe_url(BAD,fetcher=fetcher)
        good=await probe_url(HUB,fetcher=fetcher)
        assert not bad['reachable'] and bad['reason']=='dns_error'
        assert good['reachable'] and good['status']==302
        assert len(requests)==1 and requests[0].method=='GET'
        assert requests[0].headers['host']=='hub.example.com.mx'
        assert not requests[0].url.query and not requests[0].content
        assert 'PRIVATE' not in json.dumps([bad,good]) and 'unapproved.example' not in json.dumps(good)
    finally:await fetcher.close()


@pytest.mark.parametrize('status',[404,500,503])
async def test_probe_rejects_http_errors_without_retry(status):
    from backend.app.billing.portal_probe import probe_url
    from .test_browser_worker import Pages
    class Errors(Pages):
        async def fetch(self,url,headers):
            self.calls.append((url,headers));return status,{'content-type':'text/html'},b'Error'
    transport=Errors({})
    result=await probe_url(HUB,fetcher=transport)
    assert not result['reachable'] and result['reason']=='http_error' and result['status']==status
    assert len(transport.calls)==1
