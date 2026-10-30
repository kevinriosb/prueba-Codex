import re
import unicodedata
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .receipt import portal_url, purchase_date, purchase_total

REGIMES = {
    '601': 'General de Ley Personas Morales', '603': 'Personas Morales con Fines no Lucrativos',
    '605': 'Sueldos y Salarios', '606': 'Arrendamiento', '607': 'Enajenación o Adquisición de Bienes',
    '608': 'Demás ingresos', '610': 'Residentes en el Extranjero', '611': 'Dividendos',
    '612': 'Actividades Empresariales y Profesionales', '614': 'Ingresos por intereses',
    '615': 'Premios', '616': 'Sin obligaciones fiscales', '620': 'Sociedades Cooperativas de Producción',
    '621': 'Incorporación Fiscal', '622': 'Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras',
    '623': 'Opcional para Grupos de Sociedades', '624': 'Coordinados',
    '625': 'Actividades Empresariales mediante Plataformas Tecnológicas', '626': 'Régimen Simplificado de Confianza',
}
CFDI_USES = {
    'G01': 'Adquisición de mercancías', 'G02': 'Devoluciones, descuentos o bonificaciones', 'G03': 'Gastos en general',
    'I01': 'Construcciones', 'I02': 'Mobiliario y equipo de oficina', 'I03': 'Equipo de transporte',
    'I04': 'Equipo de cómputo', 'I05': 'Dados, troqueles y herramientas', 'I06': 'Comunicaciones telefónicas',
    'I07': 'Comunicaciones satelitales', 'I08': 'Otra maquinaria y equipo',
    'D01': 'Honorarios médicos y gastos hospitalarios', 'D02': 'Gastos médicos por incapacidad',
    'D03': 'Gastos funerales', 'D04': 'Donativos', 'D05': 'Intereses reales por créditos hipotecarios',
    'D06': 'Aportaciones voluntarias al SAR', 'D07': 'Primas de seguros de gastos médicos',
    'D08': 'Transportación escolar obligatoria', 'D09': 'Depósitos para el ahorro', 'D10': 'Servicios educativos',
    'S01': 'Sin efectos fiscales', 'CP01': 'Pagos', 'CN01': 'Nómina',
}
RFC = re.compile(r'^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$')


def regime_code(value):
    """Equivalencia exacta de catálogo, sin deducir el régimen del contribuyente."""
    if value is None:
        return None
    value = str(value).strip()
    if value in REGIMES:
        return value
    def key(label):
        label = ''.join(char for char in unicodedata.normalize('NFD', label.casefold())
            if unicodedata.category(char) != 'Mn')
        label = re.sub(r'^regimen\s+', '', label)
        return ' '.join(label.split())
    labels = {key(label): code for code, label in REGIMES.items()}
    labels.update({key('Sueldos y Salarios e Ingresos Asimilados a Salarios'): '605',
        key('Personas Físicas con Actividades Empresariales y Profesionales'): '612',
        key('Régimen de las Actividades Empresariales con ingresos a través de Plataformas Tecnológicas'): '625'})
    return labels.get(key(value))


def missing_fiscal_fields(data):
    checks = {'rfc': bool(RFC.fullmatch(data.get('rfc') or '')),
        'name': bool((data.get('name') or '').strip()),
        'postal_code': bool(re.fullmatch(r'[0-9]{5}', data.get('postal_code') or '')),
        'fiscal_regime': data.get('fiscal_regime') in REGIMES}
    return [field for field, valid in checks.items() if not valid]


class Status(StrEnum):
    TICKET_RECIBIDO = 'TICKET_RECIBIDO'
    DATOS_EXTRAIDOS = 'DATOS_EXTRAIDOS'
    PENDIENTE_CONFIRMACION = 'PENDIENTE_CONFIRMACION'
    CORREO_PREPARADO = 'CORREO_PREPARADO'
    CORREO_ENVIADO = 'CORREO_ENVIADO'
    ESPERANDO_RESPUESTA = 'ESPERANDO_RESPUESTA'
    FACTURA_RECIBIDA = 'FACTURA_RECIBIDA'
    FACTURA_VALIDADA = 'FACTURA_VALIDADA'
    FACTURACION_COMPLETADA = 'FACTURACION_COMPLETADA'
    REQUIERE_INTERVENCION = 'REQUIERE_INTERVENCION'
    ERROR = 'ERROR'


class SafeModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class FiscalExtraction(SafeModel):
    is_fiscal_certificate: bool
    readable: bool
    rfc: str | None = Field(max_length=13)
    name: str | None = Field(max_length=250)
    postal_code: str | None = Field(max_length=5)
    fiscal_regime: str | None = Field(max_length=160)

    @field_validator('fiscal_regime')
    @classmethod
    def normalize_regime(cls, value):
        return regime_code(value)


class PrintedAddress(SafeModel):
    url: str | None = Field(max_length=500)
    evidence: str = Field(max_length=500)
    purpose: Literal['billing','official','other','unclear']


class PrintedAddresses(SafeModel):
    printed_addresses: list[PrintedAddress] | None = Field(default=None,max_length=32)

    @model_validator(mode='after')
    def evaluate_printed_addresses(self):
        # La lista nueva es autoritativa; los documentos antiguos sin lista
        # conservan su dirección. Una URL aislada no domina su frase impresa.
        if self.printed_addresses is not None:
            from .receipt import printed_billing_addresses
            candidates,_=printed_billing_addresses([item.model_dump() for item in self.printed_addresses])
            self.billing_url=candidates[0]['url'] if candidates else None
            qr=next((item for item in self.printed_addresses if item.purpose=='billing' and item.url is None),None)
            self.billing_url_evidence=candidates[0]['evidence'] if candidates else qr.evidence if qr else None
            if hasattr(self,'merchant_website') and not self.merchant_website:
                official=next((item for item in self.printed_addresses if item.purpose=='official' and item.url
                    and item.url.removeprefix('https://').removeprefix('http://').rstrip('/').casefold() in item.evidence.casefold()),None)
                if official:
                    self.merchant_website=portal_url(official.url)
                    self.merchant_website_evidence=official.evidence
        return self


class TicketExtraction(PrintedAddresses):
    is_ticket: bool
    readable: bool
    merchant: str | None = Field(max_length=160)
    issuer_rfc: str | None = Field(max_length=13)
    purchase_date: str | None = Field(max_length=10)
    folio: str | None = Field(max_length=100)
    account_number: str | None = Field(default=None,max_length=100)
    store_number: str | None = Field(default=None,max_length=100)
    reference_number: str | None = Field(default=None,max_length=160)
    web_id: str | None = Field(default=None,max_length=160)
    toll_station: str | None = Field(default=None,max_length=160)
    lane: str | None = Field(default=None,max_length=40)
    purchase_time: str | None = Field(default=None,max_length=20)
    total: str | None = Field(max_length=24)
    currency: str | None = Field(max_length=3)
    branch: str | None = Field(max_length=160)
    billing_code: str | None = Field(max_length=160)
    billing_url: str | None = Field(max_length=500)
    billing_url_evidence: str | None = Field(default=None,max_length=500)
    merchant_website: str | None = Field(default=None,max_length=500)
    merchant_website_evidence: str | None = Field(default=None,max_length=500)
    billing_email: str | None = Field(max_length=254)
    billing_email_evidence: str | None = Field(max_length=500)
    email_explicitly_for_billing: bool

    @field_validator('purchase_date')
    @classmethod
    def valid_date(cls, value):
        return purchase_date(value)

    @field_validator('total', mode='before')
    @classmethod
    def valid_total(cls, value):
        return purchase_total(value)

    @field_validator('billing_url')
    @classmethod
    def valid_url(cls, value):
        return portal_url(value)


class TicketRouting(PrintedAddresses):
    is_ticket: bool
    readable: bool
    merchant: str | None = Field(max_length=160)
    billing_url: str | None = Field(max_length=500)
    billing_url_evidence: str | None = Field(max_length=500)
    billing_email: str | None = Field(max_length=254)
    billing_email_evidence: str | None = Field(max_length=500)
    email_explicitly_for_billing: bool
    issuer_rfc:str|None=Field(default=None,max_length=13)
    purchase_date:str|None=Field(default=None,max_length=10)
    folio:str|None=Field(default=None,max_length=100)
    account_number:str|None=Field(default=None,max_length=100)
    total:str|None=Field(default=None,max_length=24)
    currency:str|None=Field(default=None,max_length=3)
    merchant_website:str|None=Field(default=None,max_length=500)
    merchant_website_evidence:str|None=Field(default=None,max_length=500)


def validate_fiscal(data):
    return not missing_fiscal_fields(data)
