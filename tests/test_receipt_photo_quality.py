"""Fotos inventadas y extractores simulados; ninguna llamada a servicios reales."""
import json
from datetime import datetime,timezone

import pytest

from backend.app.billing.models import TicketExtraction
from backend.app.billing.receipt_quality import assess,field_total,match_photo,read_addresses
from .billing_fakes import TICKET,FISCAL,image
from .test_billing import billing_client
from .test_official_portal_discovery import Modern,modern
from .test_telegram_agent import button,owner,create
from .test_telegram_documents import confirm_profile
from .test_portal_preparation import RealBrowser
from .test_receipt_channels import qr_image


class Photos(Modern):
    def __init__(self,**fields):
        super().__init__()
        self.found=None
        self.ticket.update(billing_url=None,billing_email=None,email_explicitly_for_billing=False,
            store_number='0042',purchase_time='13:45')
        self.ticket.update(fields)

    async def extract_routing(self,mime,data):
        self.calls.append(('routing',mime))
        return TicketExtraction(**self.ticket)


def ready(factory,**fields):
    values,browser=modern(factory,Photos(**fields))
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    return values,browser


def usage(app):return app.state.telegram.requests.usage(owner(app))['analyses']


def test_unread_billing_zone_requests_a_closeup_without_silently_searching(billing_client):
    values,browser=ready(billing_client,photo_issues=[{'zone':'bottom','subject':'billing_address','reason':'too_small'}],
        field_confidence=[{'key':'billing_url','confidence':'unreadable'}])
    client,app,extractor,mail,sat,telegram,chat=values
    assert extractor.searches==[] and browser.calls==[]
    text='\n'.join(t for _,t,_ in telegram.messages)
    assert 'Acércate a la parte de abajo, donde está la dirección de facturación' in text
    assert 'No encontré una web oficial verificable' not in text
    ticket=app.state.telegram.workflow.active(owner(app))
    assert ticket['merchant']==TICKET['merchant'] and ticket['total']==TICKET['total']
    assert not ticket.get('billing_url') and mail.sent==sat.calls==[]


@pytest.mark.parametrize('total,consistent',[('116.00',True),('116.02',True),('116.03',False),('135.00',False)])
def test_amount_arithmetic_marks_review_without_rewriting_the_receipt(total,consistent):
    data=assess(TICKET|{'subtotal':'100.00','tax':'16.00','total':total,'tip':'19.00',
        'total_without_tip':'116.00','total_with_tip':'135.00'})
    assert data['amounts_consistent'] is consistent and data['total']==total
    assert data['total_without_tip']=='116.00' and data['total_with_tip']=='135.00'
    assert not consistent or field_total(data,{'key':'total','label':'Total sin propina'})=='116.00'
    assert field_total(TICKET,{'key':'total','label':'Total sin propina'}) is None


def test_low_confidence_and_inconsistent_totals_need_explicit_confirmation(billing_client):
    values,browser=ready(billing_client,total='135.00',subtotal='100.00',tax='16.00',
        field_confidence=[{'key':'folio','confidence':'low'}])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    assert ticket['amounts_consistent'] is False
    assert 'Confirma los campos de baja confianza' in telegram.messages[-1][1]
    assert 'subtotal + IVA' in telegram.messages[-1][1]
    assert field_total(ticket,{'key':'folio','label':'Ticket'}) is None
    confirm=next(data for label,data in telegram.messages[-1][2] if label=='Confirmo folio')
    chat(data=confirm,user=456)
    assert not app.state.billing.resolve_ticket(owner(app),ticket['id']).get('confidence_confirmed')
    chat(data=confirm)
    updated=app.state.billing.resolve_ticket(owner(app),ticket['id'])
    assert field_total(updated,{'key':'folio','label':'Ticket'})==ticket['folio']
    assert field_total(updated,{'key':'total','label':'Total'}) is None


def test_identical_photo_reuses_bytes_and_analysis_without_opening_a_request(billing_client):
    values,browser=ready(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    before=usage(app);calls=list(extractor.calls)
    ticket=app.state.telegram.workflow.active(owner(app))
    chat(document='receipt')
    assert usage(app)==before and extractor.calls==calls
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    assert app.state.telegram.workflow.active(owner(app))['id']==ticket['id']


def test_a_second_photo_before_the_first_analysis_waits_for_identity_instead_of_opening_a_ticket(billing_client):
    values,browser=modern(billing_client,Photos())
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    telegram.files['closeup']=image('purple');chat(document='closeup')
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    chat(data=button(telegram,'fa'))
    assert 'No hay coincidencia suficiente' in telegram.messages[-1][1]
    reads=sum(c[0] in {'routing','ticket','fields'} for c in extractor.calls)
    chat(data=button(telegram,'fm'))
    updated=app.state.telegram.workflow.active(owner(app))
    assert updated['id']==ticket['id'] and updated['is_ticket']
    assert updated['total']==TICKET['total'] and updated['store_number']=='0042'
    assert sum(c[0] in {'routing','ticket','fields'} for c in extractor.calls)==reads
    assert extractor.searches==[TICKET['merchant']] and len(app.state.billing.store.list('ticket',owner(app)))==1


def test_another_clear_photo_is_matched_before_creating_a_request_and_refunds_the_slot(billing_client):
    values,browser=ready(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app));before=usage(app)
    telegram.files['closeup']=image('blue');chat(document='closeup')
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    consent=button(telegram,'fa');calls=len(extractor.calls)
    chat(data=consent,user=456)
    assert len(extractor.calls)==calls
    chat(data=consent)
    updated=app.state.telegram.workflow.active(owner(app))
    assert updated['id']==ticket['id'] and updated['document_id']!=ticket['document_id']
    assert updated['store_number']=='0042' and updated['purchase_date']==ticket['purchase_date']
    assert len(app.state.billing.store.list('ticket',owner(app)))==1 and usage(app)==before
    assert 'Foto unida' in '\n'.join(t for _,t,_ in telegram.messages)
    calls=len(extractor.calls);chat(document='closeup')
    assert len(extractor.calls)==calls and usage(app)==before


def test_a_clearer_joined_photo_updates_quality_and_checks_the_preserved_amounts(billing_client):
    values,browser=ready(billing_client,photo_issues=[{'zone':'bottom','subject':'billing_address','reason':'too_small'}])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    extractor.ticket.update(photo_issues=[],subtotal='100.00',tax='18.00')
    telegram.files['closeup']=image('purple');chat(document='closeup');chat(data=button(telegram,'fa'))
    updated=app.state.telegram.workflow.active(owner(app))
    assert updated['id']==ticket['id'] and updated['total']==ticket['total']
    assert updated['photo_issues']==[] and updated['amounts_consistent'] is False
    assert 'subtotal + IVA' in telegram.messages[-1][1]


def test_conflicting_folio_asks_same_or_other_and_never_overwrites_known_values(billing_client):
    values,browser=ready(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app));before=usage(app)
    extractor.ticket['folio']='ORDER-DIFFERENT'
    telegram.files['closeup']=image('blue');chat(document='closeup');chat(data=button(telegram,'fa'))
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    labels=[label for label,_ in telegram.messages[-1][2]]
    assert 'Es el mismo ticket' in labels and 'Es otro' in labels
    chat(data=button(telegram,'fm'))
    updated=app.state.telegram.workflow.active(owner(app))
    assert updated['id']==ticket['id'] and updated['folio']==ticket['folio'] and usage(app)==before
    assert {'key':'folio','confidence':'low'} in updated['field_confidence']


def test_confirmed_other_uses_the_existing_reading_and_only_then_creates_a_request(billing_client):
    values,browser=ready(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    before=usage(app)
    extractor.ticket.update(merchant='Otro establecimiento genérico',folio='OTHER',purchase_date='2026-10-25')
    telegram.files['other']=image('blue');chat(document='other');chat(data=button(telegram,'fa'))
    calls=sum(k=='routing' for k,_ in extractor.calls)
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    chat(data=button(telegram,'fo'))
    assert len(app.state.billing.store.list('ticket',owner(app)))==2 and usage(app)==before+1
    assert sum(k=='routing' for k,_ in extractor.calls)==calls
    assert app.state.telegram.workflow.active(owner(app))['merchant']=='Otro establecimiento genérico'


def test_quota_rejection_does_not_reserve_an_attempt_and_confirmed_union_can_continue(billing_client):
    values,browser=ready(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing;user=owner(app)
    with billing.database.connection(True) as conn:
        for i in range(billing.settings.billing_max_daily_extractions-usage(app)):
            conn.execute('INSERT INTO billing_extractions VALUES(?,?,?,?)',(user,datetime.now(timezone.utc).date().isoformat(),'used-'+str(i),'complete'))
    calls=len(extractor.calls);before=usage(app)
    telegram.files['closeup']=image('blue');chat(document='closeup');chat(data=button(telegram,'fa'))
    assert len(extractor.calls)==calls and usage(app)==before
    assert 'no analicé ni consumí un intento' in telegram.messages[-1][1]
    chat(data=button(telegram,'fm'));chat(data=button(telegram,'xa'))
    assert usage(app)==before and len(billing.store.list('ticket',user))==1
    assert billing.settings.billing_max_daily_extractions==10


def test_qr_decoding_has_priority_and_mismatch_requires_visible_domain_review(billing_client):
    visual='https://biling.example/portal?secret=hidden'
    qr='https://billing.example/portal?secret=hidden'
    extractor=Photos(printed_addresses=[{'url':visual,'evidence':'Facturación: '+visual,'purpose':'billing'}])
    browser=RealBrowser()
    values=create(billing_client,extractor=extractor,browser=browser)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    telegram.files['receipt']=qr_image(qr)
    chat(document='receipt');chat(data=button(telegram,'an'))
    updated=app.state.telegram.workflow.active(owner(app))
    assert updated['qr_text_mismatch'] and updated['billing_url']==qr
    text=telegram.messages[-1][1]
    assert 'biling.example/portal' in text and 'billing.example/portal' in text and 'difieren' in text
    assert 'secret=' not in text and 'hidden' not in text
    assert browser.calls==[] and button(telegram,'wa')


@pytest.mark.parametrize('purpose,response,result',[
    ('billing',{'reachable':True,'reason':None},'valid'),
    ('billing',{'reachable':False,'reason':'timeout'},'no_response'),
    ('billing',{'reachable':False,'reason':'private_address'},'blocked'),
    ('other',{'reachable':True,'reason':None},'not_billing'),
])
def test_every_reported_address_has_a_private_safe_diagnostic(billing_client,caplog,purpose,response,result):
    url='https://portal.example/access?folio=SECRET&email=PRIVATE'
    extractor=Photos(printed_addresses=[{'url':url,'evidence':'Facturación '+url,'purpose':purpose}])
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    async def probe(url):return {'url':url,'status':200,**response}
    app.state.billing.portal_discovery.probe=probe
    with caplog.at_level('INFO',logger='personal_agent.portal'):chat(data=button(telegram,'an'))
    logs=[json.loads(r.message) for r in caplog.records if r.name=='personal_agent.portal' and json.loads(r.message).get('tool')=='printed_address']
    assert any(l['address_purpose']==purpose and l['address_result']==result for l in logs)
    text=json.dumps(logs)
    for secret in ('SECRET','PRIVATE','folio=',FISCAL['rfc'],FISCAL['name'],TICKET['folio'],TICKET['total']):assert secret not in text
    if result!='valid':
        text='\n'.join(t for _,t,_ in telegram.messages)
        assert 'No usé portal.example/access:' in text and 'SECRET' not in text


def test_matching_needs_several_independent_values_and_rejects_conflicts():
    saved=TICKET|{'id':'one','store_number':'0042'}
    assert match_photo(TICKET,[saved])[0]==saved
    assert match_photo({'merchant':saved['merchant'],'total':saved['total']},[saved])[0] is None
    assert match_photo(TICKET|{'store_number':'0099'},[saved])[0] is None
    assert match_photo(TICKET,[saved,saved|{'id':'two'}])[0] is None
    assert match_photo(TICKET|{'field_confidence':[{'key':'folio','confidence':'low'}]},[saved])[0] is None


def test_address_diagnostics_reject_fiscal_content_even_if_a_caller_skips_sanitizing():
    from backend.app.billing.portal_diagnostics import validate_address
    for key,value in [('path','/'+FISCAL['rfc']),('host','cliente@example.com'),('path','/116.00'),('address_result',TICKET['folio'])]:
        event={'address_purpose':'billing','address_result':'valid','host':'portal.example','path':'/access'}
        event[key]=value
        with pytest.raises(ValueError):validate_address(event)


def test_all_portal_url_displays_remove_query_and_fragment_without_changing_the_destination(billing_client):
    from .test_portal_preparation import prepared
    from backend.app.billing.receipt_quality import display_address
    url='https://portal.example/form?folio=SECRET#TOKEN'
    assert display_address(url)=='https://portal.example/form'
    values,browser,_=prepared(billing_client,require_ready=True)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    app.state.billing.store.add('portal_memory',owner(app),{'merchant':ticket['merchant'],'url':url})
    chat('/portales')
    assert 'https://portal.example/form' in telegram.messages[-1][1]
    assert 'SECRET' not in telegram.messages[-1][1] and 'TOKEN' not in telegram.messages[-1][1]
    assert app.state.billing.store.list('portal_memory',owner(app))[0]['url']==url


def test_a_survey_address_reports_the_filter_instead_of_claiming_a_network_failure(billing_client,caplog):
    url='https://portal.example/survey?token=PRIVATE'
    with caplog.at_level('INFO',logger='personal_agent.portal'):
        values,browser=ready(billing_client,printed_addresses=[{'url':url,'evidence':'Facturación '+url,'purpose':'billing'}])
    events=[json.loads(r.message) for r in caplog.records if r.name=='personal_agent.portal']
    assert any(e.get('address_result')=='survey_filtered' for e in events)
    telegram=values[-2]
    text='\n'.join(t for _,t,_ in telegram.messages)
    assert 'rechazada por el filtro de encuestas' in text and 'PRIVATE' not in text
    assert browser.calls==[]


def test_an_old_confidence_button_never_confirms_a_changed_value(billing_client):
    values,browser=ready(billing_client,field_confidence=[{'key':'folio','confidence':'low'}])
    client,app,extractor,mail,sat,telegram,chat=values
    confirm=button(telegram,'qc')
    ticket=app.state.telegram.workflow.active(owner(app))
    app.state.billing.store.update('ticket',owner(app),ticket['id'],ticket|{'folio':'CHANGED'})
    chat(data=confirm)
    assert 'Cambió el valor' in telegram.messages[-1][1]
    assert not app.state.billing.resolve_ticket(owner(app),ticket['id']).get('confidence_confirmed')


def test_initial_ambiguity_stays_null_and_existing_form_directed_reading_is_preserved(billing_client):
    from .test_portal_preparation import prepared,ScopedExtractor
    from .test_portal_submission import enable_transactions,PortalPages
    class Ambiguous(ScopedExtractor):
        async def extract_routing(self,*args):
            result=await super().extract_routing(*args)
            return TicketExtraction.model_validate(result.model_dump()|{'folio':None,'field_confidence':[{'key':'folio','confidence':'low'}]})
        async def extract_fields(self,mime,data,fields):
            result=await super().extract_fields(mime,data,fields)
            return result.model_copy(update={'folio':TICKET['folio']} if any(f['key']=='folio' for f in fields) else {})
    values,browser,_=prepared(billing_client,extractor=Ambiguous(),require_ready=True)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    document=app.state.billing.document(owner(app),ticket['document_id'])
    assert document['extracted']['folio'] is None and ticket['folio']==TICKET['folio']
    assert [f['key'] for f in next(c for c in extractor.calls if c[0]=='fields')[1]]==['folio','store_number','purchase_date']
    enable_transactions(browser,PortalPages())
    before=len(extractor.calls)
    chat(data='lv:'+ticket['id'])
    assert len(extractor.calls)==before and mail.sent==sat.calls==[]


def test_joining_a_photo_keeps_a_private_form_and_its_two_approvals_unchanged(billing_client):
    from .test_portal_steps_cycle import configured,LAYOUTS,complete_missing
    values,browser,pages=configured(billing_client,LAYOUTS[1])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    chat(data='lv:'+ticket['id']);complete_missing(values)
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='review' and tx['private']
    posts=len([r for r in pages.sent if r[0]=='POST'])
    telegram.files['closeup']=image('purple');chat(document='closeup')
    chat(data=button(telegram,'fm'))
    assert app.state.billing.portal_submission.current(owner(app),ticket['id'])==tx
    assert app.state.billing.resolve_ticket(owner(app),ticket['id'])['document_id']==ticket['document_id']
    assert len([r for r in pages.sent if r[0]=='POST'])==posts
    assert mail.sent==sat.calls==[]
