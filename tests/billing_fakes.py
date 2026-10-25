import asyncio
import base64
import io

from PIL import Image
from pypdf import PdfWriter

from backend.app.billing.models import FiscalExtraction, TicketExtraction
from backend.app.billing.providers import Delivery, Reply

KEY=base64.urlsafe_b64encode(b'k'*32).decode()
FISCAL={'is_fiscal_certificate':True,'readable':True,'rfc':'AAA010101AAA','name':'EMPRESA DEMO SA DE CV','postal_code':'01000','fiscal_regime':'601'}
TICKET={'is_ticket':True,'readable':True,'merchant':'Comercio de prueba','issuer_rfc':'BBB010101BBB',
    'purchase_date':'2026-10-24','folio':'T-1234','total':'116.00','currency':'MXN','branch':'Centro',
    'billing_code':None,'billing_url':None,'billing_email':'facturacion@example.com',
    'billing_email_evidence':'Facturación CFDI: facturacion@example.com','email_explicitly_for_billing':True}


def image(color='white'):
    data=io.BytesIO()
    Image.new('RGB',(16,16),color).save(data,format='PNG')
    return data.getvalue()


def pdf():
    data=io.BytesIO()
    writer=PdfWriter()
    writer.add_blank_page(width=100,height=100)
    writer.write(data)
    return data.getvalue()


def xml(**replacements):
    values={'rfc':'AAA010101AAA','name':'EMPRESA DEMO SA DE CV','postal_code':'01000','regime':'601','use':'G03','total':'116.00'}
    values.update(replacements)
    return f'''<?xml version="1.0"?><cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" Version="4.0" TipoDeComprobante="I" Fecha="2026-10-24T12:00:00" Moneda="MXN" SubTotal="100.00" Total="{values['total']}" Sello="ZGVtbw==" Certificado="ZGVtbw==">
    <cfdi:Emisor Rfc="BBB010101BBB" Nombre="COMERCIO DEMO" RegimenFiscal="601"/>
    <cfdi:Receptor Rfc="{values['rfc']}" Nombre="{values['name']}" DomicilioFiscalReceptor="{values['postal_code']}" RegimenFiscalReceptor="{values['regime']}" UsoCFDI="{values['use']}"/>
    <cfdi:Conceptos><cfdi:Concepto ClaveProdServ="01010101" Cantidad="1" ClaveUnidad="ACT" Descripcion="Prueba" ValorUnitario="100.00" Importe="100.00" ObjetoImp="02"/></cfdi:Conceptos>
    <cfdi:Complemento><tfd:TimbreFiscalDigital Version="1.1" UUID="11111111-1111-4111-8111-111111111111" SelloSAT="ZGVtbw==" SelloCFD="ZGVtbw==" NoCertificadoSAT="00001000000500000000"/></cfdi:Complemento></cfdi:Comprobante>'''.encode()


class FakeExtractor:
    def __init__(self):
        self.fiscal=FISCAL.copy()
        self.ticket=TICKET.copy()
        self.calls=[]

    async def extract(self,purpose,mime,data):
        self.calls.append((purpose,mime))
        return FiscalExtraction(**self.fiscal) if purpose=='csf' else TicketExtraction(**self.ticket)


class FakeMail:
    configured=True
    def __init__(self):
        self.connected=True
        self.sent=[]
        self.replies=[]
        self.failure=None
        self.fetch_failure=None
        self.fetch_calls=0
        self.reply_text=None
        self.started=None
        self.release=None

    def ready(self,owner):
        return self.connected

    def account(self,owner):
        return {'email':'agente@example.com'} if self.connected else None

    async def send(self,owner,message,*,thread_id=None):
        self.sent.append((owner,message))
        if self.started:
            self.started.set()
            await self.release.wait()
        if self.failure:
            raise self.failure
        return Delivery('gmail_message_'+str(len(self.sent)),thread_id or 'gmail_thread')

    async def fetch(self,owner,ticket):
        self.fetch_calls+=1
        if self.fetch_failure:
            raise self.fetch_failure
        if self.reply_text:
            return [Reply('response-demo','facturacion@example.com',ticket['delivery']['rfc_message_id'],self.reply_text)]
        return list(self.replies)

    async def close(self):
        pass


class FakeSat:
    def __init__(self):
        self.calls=[]
        self.vigente=True

    async def verify(self,document):
        self.calls.append(document.copy())
        return {'vigente':self.vigente,'query_success':True,'status':'Vigente' if self.vigente else 'Cancelado'}

    async def close(self):
        pass


class FakeTelegram:
    def __init__(self):
        self.messages=[]
        self.files={}
        self.downloads=[]

    async def send(self,chat_id,text,buttons=None):
        self.messages.append((chat_id,text,buttons))

    async def download(self,file_id):
        self.downloads.append(file_id)
        return self.files[file_id]

    async def send_document(self,chat_id,document):
        self.messages.append((chat_id,'document:'+document['id'],None))

    async def close(self):
        pass
