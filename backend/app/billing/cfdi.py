import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx2
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from ..errors import AppError

CFDI='{http://www.sat.gob.mx/cfd/4}'
TFD='{http://www.sat.gob.mx/TimbreFiscalDigital}'


def validate_cfdi(data,profile,ticket):
    if len(data)>1_048_576:
        raise AppError(413,'xml_size','El XML supera el límite permitido.')
    try:
        root=ElementTree.fromstring(data,forbid_dtd=True)
        if root.tag!=CFDI+'Comprobante' or root.get('Version')!='4.0' or root.get('TipoDeComprobante')!='I':
            raise ValueError()
        emitters=root.findall(CFDI+'Emisor')
        receivers=root.findall(CFDI+'Receptor')
        stamps=root.findall(CFDI+'Complemento/'+TFD+'TimbreFiscalDigital')
        if len(emitters)!=1 or len(receivers)!=1 or len(stamps)!=1:
            raise ValueError()
        issuer,recipient,stamp=emitters[0],receivers[0],stamps[0]
        identifier=stamp.get('UUID','').upper()
        if not re.fullmatch(r'[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}',identifier) or stamp.get('Version')!='1.1':
            raise ValueError()
        if recipient.get('Rfc','').upper()!=profile['rfc'] or recipient.get('DomicilioFiscalReceptor')!=profile['postal_code']:
            raise ValueError()
        if recipient.get('RegimenFiscalReceptor')!=profile['fiscal_regime'] or recipient.get('UsoCFDI')!=profile['cfdi_use']:
            raise ValueError()
        normalize=lambda value:' '.join(value.upper().split())
        if normalize(recipient.get('Nombre',''))!=normalize(profile['name']):
            raise ValueError()
        if ticket.get('issuer_rfc') and issuer.get('Rfc')!=ticket['issuer_rfc']:
            raise ValueError()
        if Decimal(root.get('Total','NaN'))!=Decimal(ticket['total']) or not root.get('Sello') or not root.get('Certificado'):
            raise ValueError()
        if not stamp.get('SelloSAT') or not stamp.get('SelloCFD') or not stamp.get('NoCertificadoSAT'):
            raise ValueError()
        if ticket.get('currency') and root.get('Moneda')!=ticket['currency']:
            raise ValueError()
        return {'uuid':identifier,'issuer_rfc':issuer.get('Rfc'),'recipient_rfc':recipient.get('Rfc'),
            'total':root.get('Total'),'structural_match':True,'sat_verified':False,
            'signature_cryptographically_verified':False}
    except (ValueError,InvalidOperation,ElementTree.ParseError,DefusedXmlException,TypeError):
        raise AppError(422,'cfdi_invalid','El XML no corresponde a un CFDI 4.0 de ingreso compatible con este ticket y perfil.') from None


class SatVerifier:
    """Consulta de vigencia en el destino fijo del SAT; no equivale a verificar firmas."""
    def __init__(self,enabled,*,client=None):
        self.enabled=enabled
        self.client=client or httpx2.AsyncClient(timeout=15,follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def verify(self,document):
        if not self.enabled:
            raise AppError(503,'sat_not_configured','La verificación de vigencia ante el SAT está desactivada.')
        query='?'+urlencode({'re':document['issuer_rfc'],'rr':document['recipient_rfc'],
            'tt':document['total'],'id':document['uuid']})
        envelope='<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><Consulta xmlns="http://tempuri.org/"><expresionImpresa>'+escape(query)+'</expresionImpresa></Consulta></s:Body></s:Envelope>'
        try:
            response=await self.client.post('https://consultaqr.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc',
                content=envelope.encode(),headers={'Content-Type':'text/xml; charset=utf-8','SOAPAction':'"http://tempuri.org/IConsultaCFDIService/Consulta"'})
            if response.status_code!=200 or len(response.content)>100_000:
                raise ValueError()
            root=ElementTree.fromstring(response.content,forbid_dtd=True)
            fields={node.tag.rsplit('}',1)[-1]:(node.text or '').strip() for node in root.iter()}
            return {'vigente':fields.get('Estado')=='Vigente','status':fields.get('Estado','Desconocido'),
                'query_success':fields.get('CodigoEstatus','').startswith('S -')}
        except Exception:
            raise AppError(502,'sat_unavailable','No se pudo comprobar la vigencia ante el SAT. Requiere revisión; no se reintentará automáticamente.') from None
