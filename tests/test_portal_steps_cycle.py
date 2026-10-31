"""Portales inventados: número y orden de formularios variables, sin red real."""
import pytest

from backend.browser_worker.engine import BrowserManager
from .test_portal_submission import PortalPages,FORM,FISCAL,submission_args,confirmed_send


async def test_new_form_is_not_unknown_when_an_auxiliary_response_fails():
    class Pages(PortalPages):
        async def request_limited(self,method,url,headers,body=None,*,on_bytes=None):
            if url.endswith('/optional.js'):
                return 404,{'content-type':'application/javascript'},b''
            return await super().request_limited(method,url,headers,body,on_bytes=on_bytes)
    pages=Pages();pages.responses['/lookup']=FISCAL+'<script src="/optional.js"></script>'
    manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner','https://merchant.example/',['https://merchant.example'])
        view=await manager.transaction_view('owner',opened['session_id'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**submission_args(view,['AAA010101AAA','T-1234']))
        assert [f['label'] for f in result['view']['snapshot']['fields']]==['Nombre fiscal','CP fiscal','Uso CFDI']
        assert result['state']=='review'
    finally:await manager.close()

from urllib.parse import parse_qs,urlsplit
from backend.app.billing.models import TicketExtraction
from .test_billing import billing_client
from .test_portal_preparation import prepared,ScopedExtractor,ROOT
from .test_portal_submission import enable_transactions
from .test_telegram_agent import button,owner
from .billing_fakes import TICKET,xml,pdf

LABELS={'rfc':'RFC','folio':'Ticket','store_number':'Tienda','purchase_date':'Fecha','purchase_time':'Hora',
    'register_number':'Caja','payment_method':'Forma de pago','name':'Nombre fiscal','postal_code':'CP fiscal',
    'email':'Correo','fiscal_regime':'Régimen fiscal','cfdi_use':'Uso CFDI','first_name':'Nombre(s)',
    'family_names':'Apellidos','person_type':'Tipo de persona'}
FIELDS={**TICKET,'store_number':'07001','purchase_time':'12:34','register_number':'04','payment_method':'01'}
LAYOUTS=[
    [['rfc','folio','purchase_time','name','email','postal_code','cfdi_use']],
    [['rfc','folio','store_number','purchase_date'],['person_type','first_name','family_names','postal_code','fiscal_regime','cfdi_use','email','purchase_time']],
    [['rfc','email'],['folio','payment_method','register_number'],['postal_code','name','cfdi_use']],
    [['rfc','folio'],['postal_code','fiscal_regime'],['purchase_time','payment_method','register_number'],['name','email','cfdi_use']],
]


class Directed(ScopedExtractor):
    async def extract_fields(self,mime,data,fields):
        self.calls.append(('fields',[{'key':f['key'],'label':f['label']} for f in fields]))
        values={key:None for key in TicketExtraction.model_fields}
        values.update(is_ticket=True,readable=True,email_explicitly_for_billing=False)
        values.update({f['key']:FIELDS[f['key']] for f in fields})
        return TicketExtraction.model_validate(values)


def form(keys,target):
    controls=[]
    for key in keys:
        if key in {'person_type','cfdi_use'}:
            options=('<option value=F>Persona física</option><option value=M>Persona moral</option>' if key=='person_type' else
                '<option value=G03>G03 - Gastos en general</option>')
            control='<select name="'+key+'" required><option value="">Elige</option>'+options+'</select>'
        else:control='<input name="'+key+'" required>'
        controls.append('<label>'+LABELS[key]+control+'</label>')
    return '<form action="'+target+'" method="post">'+''.join(controls)+'<button>Enviar</button></form>'


class MultiPages(PortalPages):
    def __init__(self,layout,*,navigation=False,unsafe_navigation=False):
        self.layout=layout
        super().__init__(form(layout[0],'/step-a'))
        self.pages={ROOT:form(layout[0],'/step-a')}
        self.responses={}
        self.invoice_xml=xml();self.invoice_pdf=pdf()
        for i,keys in enumerate(layout):
            target='/step-'+chr(ord('a')+i)
            if i+1==len(layout):self.responses[target]='<p>Factura generada</p><a href="/invoice.xml">XML</a><a href="/invoice.pdf">PDF</a>'
            else:self.responses[target]=form(layout[i+1],'/step-'+chr(ord('a')+i+1))
        if navigation:
            next_form=self.responses['/step-a']
            self.responses['/step-a']='<a href="/bridge">Facturación</a>'
            self.responses['/bridge']=next_form
        if unsafe_navigation:
            next_form=self.responses['/step-a']
            self.responses['/step-a']='<form action="/bridge" method="post"><input type=hidden name=prior value=already-private><button>Continuar</button></form>'
            self.responses['/bridge']=next_form


def configured(factory,layout,**options):
    pages=MultiPages(layout,**options)
    def configure(app,ticket):
        browser=app.state.billing.browser
        browser.pages=pages;browser.manager.fetcher=pages
        app.state.billing.complete_ticket_field(owner(app),ticket['id'],'total','116.00')
    values,browser,model=prepared(factory,extractor=Directed(),before_portal=configure)
    enable_transactions(browser,pages)
    return values,browser,pages


def complete_missing(values):
    client,app,extractor,mail,sat,telegram,chat=values
    for _ in range(15):
        tx=app.state.billing.portal_submission.current(owner(app),app.state.telegram.workflow.active(owner(app))['id'])
        approvals,missing,values_to_send=app.state.billing.portal_submission.approvals(owner(app),tx)
        if not missing:return tx
        field=missing[0]
        assert field.get('reference'),missing
        chat(data='lm:'+tx['id']+':'+field['reference'])
        explicit={'person_type':'Persona moral','first_name':'Nombre proporcionado','family_names':'Apellidos proporcionados','email':'entrega@example.com'}
        chat(explicit.get(field.get('key'),'Dato proporcionado'))
    pytest.fail('No se completaron los campos explícitos')


@pytest.mark.parametrize('layout',LAYOUTS,ids=['one','two','three','four'])
def test_each_step_observes_reads_only_missing_fields_and_requires_two_separate_clicks(billing_client,layout):
    values,browser,pages=configured(billing_client,layout)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    chat(data='lv:'+ticket['id'])
    for step in range(len(layout)):
        tx=complete_missing(values)
        assert tx['state']=='review'
        session=browser.manager.sessions[tx['session_id']]
        assert client.portal.call(session.page.locator('input:not([type=hidden]),select,textarea').evaluate_all,'nodes=>nodes.map(e=>e.value)')==['']*len(layout[step])
        before=len([r for r in pages.sent if r[0]=='POST'])
        first=button(telegram,'ls')
        chat(data=first)
        assert app.state.billing.portal_submission.current(owner(app),ticket['id'])['state']=='prepared'
        assert len([r for r in pages.sent if r[0]=='POST'])==before
        chat(data=first)  # Repetir la autorización de llenado NO envía.
        assert len([r for r in pages.sent if r[0]=='POST'])==before
        final=button(telegram,'lc');chat(data=final)
        assert len([r for r in pages.sent if r[0]=='POST'])==before+1
        chat(data=final)  # Repetir confirmación no repite el POST.
        assert len([r for r in pages.sent if r[0]=='POST'])==before+1
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='submitted'
    ticket=app.state.billing.resolve_ticket(owner(app),ticket['id'])
    assert len(ticket['invoice_documents'])==2 and ticket['validation']['structural_match']
    assert not ticket['validation']['sat_verified'] and sat.calls==mail.sent==[]
    chat(data='sv:'+ticket['id'])
    assert len(sat.calls)==1
    requested=[f['key'] for kind,fields in extractor.calls if kind=='fields' for f in fields]
    expected={k for keys in layout for k in keys if k in {'folio','store_number','purchase_date','purchase_time','register_number','payment_method'}}
    assert set(requested)==expected
    assert len(requested)==len(set(requested))


@pytest.mark.parametrize('unsafe',[False,True],ids=['public-get','private-post'])
def test_form_and_navigation_pages_alternate_without_implicit_fiscal_approval(billing_client,unsafe):
    values,browser,pages=configured(billing_client,LAYOUTS[1],navigation=not unsafe,unsafe_navigation=unsafe)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    chat(data='lv:'+ticket['id']);complete_missing(values)
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='navigation' and len([r for r in pages.sent if r[0]=='POST'])==1
    assert tx['stop_reason']=='private_data_may_be_retained'
    chat(data=button(telegram,'ls'))
    assert len([r for r in pages.sent if r[0]=='POST'])==1
    chat(data=button(telegram,'lc'))
    tx=complete_missing(values)
    assert tx['state']=='review' and [f['key'] for f in tx['view']['snapshot']['fields']]==LAYOUTS[1][1]
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    assert app.state.billing.portal_submission.current(owner(app),ticket['id'])['state']=='submitted'


def test_missing_profile_email_is_explicit_and_does_not_read_it_from_the_receipt(billing_client):
    values,browser,pages=configured(billing_client,[['rfc','email'],['folio']])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    profile=app.state.billing.store.get('profile',owner(app),ticket['profile_id'])
    app.state.billing.store.update('profile',owner(app),profile['id'],profile|{'email':None})
    before=list(extractor.calls)
    chat(data='lv:'+ticket['id'])
    assert 'Falta: Correo' in telegram.messages[-1][1]
    assert not any(data.startswith('ls:') for _,data in telegram.messages[-1][2])
    assert extractor.calls==before and pages.sent==[]
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    field=next(f for f in tx['view']['snapshot']['fields'] if f['key']=='email')
    chat(data='lm:'+tx['id']+':'+field['reference']);chat('correo inválido')
    assert not app.state.billing.portal_submission.approvals(owner(app),app.state.billing.portal_submission.current(owner(app),ticket['id']))[0]
    chat(data='lm:'+tx['id']+':'+field['reference']);chat('entrega@example.com')
    chat(data=button(telegram,'ls'))
    assert pages.sent==[]
    chat(data=button(telegram,'lc'))
    assert b'email=entrega%40example.com' in pages.sent[0][3]


def lost_step(factory):
    values,browser,pages=configured(factory,LAYOUTS[1])
    client,app,extractor,mail,sat,telegram,chat=values
    original=browser.transaction_send
    async def lost(*args,**kwargs):
        result=await original(*args,**kwargs)
        if not kwargs.get('prepare_only'):raise AppError(502,'portal_send_unknown','Respuesta no disponible')
        return result
    browser.transaction_send=lost
    ticket=app.state.telegram.workflow.active(owner(app))
    chat(data='lv:'+ticket['id']);chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='unknown'
    return values,browser,pages,ticket,tx,original


def test_status_recovers_a_new_form_without_another_write_or_post(billing_client):
    values,browser,pages,ticket,tx,original=lost_step(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    count=len(pages.sent)
    chat(data='lq:'+ticket['id'])
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='review' and len(pages.sent)==count
    assert [f['key'] for f in tx['view']['snapshot']['fields']]==LAYOUTS[1][1]
    assert not any(r[0]=='POST' for r in pages.sent[1:])
    chat(data='lq:'+ticket['id'])
    assert 'No hay cambios' in telegram.messages[-1][1]


@pytest.mark.parametrize('choice',['restart','manual','email'])
def test_unknown_and_expired_session_has_clear_nonautomatic_exits(billing_client,choice):
    values,browser,pages,ticket,tx,original=lost_step(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    client.portal.call(browser.manager.close_session,owner(app),tx['session_id'])
    count=len(pages.sent)
    chat(data='lq:'+ticket['id'])
    assert 'sesión del navegador venció' in telegram.messages[-1][1]
    assert all(any(data.startswith(prefix) for _,data in telegram.messages[-1][2]) for prefix in ['lx:','le:','lz:'])
    chat(data='lq:'+ticket['id'])
    assert 'No hay cambios' in telegram.messages[-1][1] and len(pages.sent)==count
    if choice=='email':
        chat(data='le:'+ticket['id'])
        assert 'spam' in telegram.messages[-1][1] and mail.sent==[]
    elif choice=='manual':
        chat(data='lz:'+ticket['id'])
        assert app.state.billing.portal_submission.current(owner(app),ticket['id'])['state']=='resolved'
        assert not app.state.billing.resolve_ticket(owner(app),ticket['id']).get('validation')
    else:
        browser.transaction_send=original
        grants=[g['id'] for g in app.state.billing.store.list('portal_grant',owner(app))]
        chat(data='lx:'+ticket['id'])
        assert 'duplicar' in telegram.messages[-1][1] and len(pages.sent)==count
        confirm=button(telegram,'ly')
        chat(data=confirm,user=456)
        assert app.state.billing.portal_submission.current(owner(app),ticket['id'])['state']=='unknown'
        chat(data=confirm)
        updated=app.state.billing.portal_submission.current(owner(app),ticket['id'])
        assert updated['state']=='review' and updated['session_id']!=tx['session_id']
        assert len(pages.sent)==count and [g['id'] for g in app.state.billing.store.list('portal_grant',owner(app))]==grants
        chat(data=confirm)
        assert app.state.billing.portal_submission.current(owner(app),ticket['id'])['session_id']==updated['session_id']


def test_configurable_limit_stops_before_an_extra_submission(billing_client):
    from functools import partial
    values,browser,pages=configured(partial(billing_client,billing_portal_max_steps=2),LAYOUTS[3])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app));chat(data='lv:'+ticket['id'])
    for _ in range(2):
        complete_missing(values)
        chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='step_limit' and len([r for r in pages.sent if r[0]=='POST'])==2
    assert 'límite de pasos' in telegram.messages[-1][1]
    assert not any(data.startswith(('ls:','lc:')) for _,data in telegram.messages[-1][2])


def test_send_without_the_fill_approval_and_changed_data_never_dispatch(billing_client):
    values,browser,pages=configured(billing_client,LAYOUTS[0])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app));chat(data='lv:'+ticket['id']);complete_missing(values)
    fill=button(telegram,'ls')
    chat(data=fill.replace('ls:','lc:'))
    assert pages.sent==[]
    chat(data=fill);final=button(telegram,'lc')
    profile=app.state.billing.store.get('profile',owner(app),ticket['profile_id'])
    app.state.billing.store.update('profile',owner(app),profile['id'],profile|{'email':'changed@example.test'})
    chat(data=final)
    assert pages.sent==[]


def test_validation_message_is_shown_without_an_automatic_retry(billing_client):
    values,browser,pages=configured(billing_client,LAYOUTS[1])
    client,app,extractor,mail,sat,telegram,chat=values
    pages.responses['/step-a']='<p role=alert>El dato no coincide; revísalo.</p>'+form(LAYOUTS[1][0],'/step-a')
    ticket=app.state.telegram.workflow.active(owner(app));chat(data='lv:'+ticket['id'])
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='correction' and 'El dato no coincide' in telegram.messages[-1][1]
    count=len(pages.sent);chat(data='lq:'+ticket['id'])
    assert len(pages.sent)==count and 'No hay cambios' in telegram.messages[-1][1]


def test_an_unclassified_unchanged_form_is_uncertain_and_never_retried(billing_client):
    values,browser,pages=configured(billing_client,LAYOUTS[1])
    client,app,extractor,mail,sat,telegram,chat=values
    pages.responses['/step-a']=form(LAYOUTS[1][0],'/step-a')
    ticket=app.state.telegram.workflow.active(owner(app));chat(data='lv:'+ticket['id'])
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='unknown' and tx['view']['reason']=='unclassified'
    count=len(pages.sent)
    chat(data='lq:'+ticket['id']);chat(data='lq:'+ticket['id'])
    assert len(pages.sent)==count and 'No hay cambios' in telegram.messages[-1][1]


def test_download_after_a_pure_navigation_step_still_receives_and_validates_files(billing_client):
    values,browser,pages=configured(billing_client,[['rfc','folio']])
    client,app,extractor,mail,sat,telegram,chat=values
    pages.responses['/step-a']='<a href="/downloads">Facturación</a>'
    pages.responses['/downloads']='<a href="/invoice.xml">XML</a><a href="/invoice.pdf">PDF</a>'
    ticket=app.state.telegram.workflow.active(owner(app));chat(data='lv:'+ticket['id'])
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    assert app.state.billing.portal_submission.current(owner(app),ticket['id'])['state']=='navigation'
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    current=app.state.billing.resolve_ticket(owner(app),ticket['id'])
    assert current['validation']['structural_match'] and len(current['invoice_documents'])==2
    assert sat.calls==mail.sent==[]


def test_a_truncated_final_review_never_offers_the_send_confirmation(billing_client,monkeypatch):
    keys=['rfc']
    for letter in 'abcdefghijklmnopqrstuvw':
        key='detail_'+letter
        keys.append(key);monkeypatch.setitem(LABELS,key,'Dato '+letter)
    values,browser,pages=configured(billing_client,[keys])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app));chat(data='lv:'+ticket['id'])
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    for field in tx['view']['snapshot']['fields']:
        if field.get('key') is None:tx=app.state.billing.portal_submission.set_field(owner(app),tx['id'],field['reference'],'x'*240)
    approvals,missing,_=app.state.billing.portal_submission.approvals(owner(app),tx)
    assert not missing
    # Preparación explícita del test; ningún dato sale mientras la red está cerrada.
    tx=client.portal.call(app.state.billing.portal_submission.prepare,owner(app),approvals[0]['id'])
    assert tx['state']=='prepared' and pages.sent==[]
    client.portal.call(app.state.telegram.submission.show,owner(app),123,ticket['id'])
    assert 'exceden la vista' in telegram.messages[-1][1]
    assert not any(data.startswith(('ls:','lc:')) for _,data in telegram.messages[-1][2])


def test_fields_revealed_by_filling_require_a_fresh_review_before_sending(billing_client):
    values,browser,pages=configured(billing_client,[['rfc','folio']])
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    tx=None
    chat(data='lv:'+ticket['id'])
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    page=browser.manager.sessions[tx['session_id']].page
    client.portal.call(page.evaluate,"""() => document.querySelector('[name=rfc]').addEventListener('input',()=>{
        if(!document.querySelector('[name=register_number]'))document.querySelector('form').insertAdjacentHTML('afterbegin','<label>Caja<input name=register_number></label>');
    })""")
    before=len(extractor.calls)
    chat(data=button(telegram,'ls'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert tx['state']=='correction' and pages.sent==[]
    assert 'formulario cambió' in telegram.messages[-1][1]
    assert [f['key'] for f in extractor.calls[-1][1]]==['register_number'] and len(extractor.calls)==before+1
    assert not any(data.startswith('lc:') for _,data in telegram.messages[-1][2])
    chat(data=button(telegram,'ls'))
    assert pages.sent==[]
    chat(data=button(telegram,'lc'))
    assert b'register_number=04' in pages.sent[0][3]
