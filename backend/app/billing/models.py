import re
import unicodedata
from datetime import date
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class TicketExtraction(SafeModel):
    is_ticket: bool
    readable: bool
    merchant: str | None = Field(max_length=160)
    issuer_rfc: str | None = Field(max_length=13)
    purchase_date: str | None = Field(max_length=10)
    folio: str | None = Field(max_length=100)
    total: str | None = Field(max_length=24)
    currency: str | None = Field(max_length=3)
    branch: str | None = Field(max_length=160)
    billing_code: str | None = Field(max_length=160)
    billing_url: str | None = Field(max_length=500)
    billing_email: str | None = Field(max_length=254)
    billing_email_evidence: str | None = Field(max_length=500)
    email_explicitly_for_billing: bool

    @field_validator('purchase_date')
    @classmethod
    def valid_date(cls, value):
        if value:
            date.fromisoformat(value)
        return value

    @field_validator('total')
    @classmethod
    def valid_total(cls, value):
        if value is not None:
            if not re.fullmatch(r'[0-9]{1,10}(?:\.[0-9]{1,2})?', value) or Decimal(value) <= 0:
                raise ValueError('Importe no válido')
        return value

    @field_validator('billing_url')
    @classmethod
    def valid_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Portal no válido')
        return value


def validate_fiscal(data):
    return not missing_fiscal_fields(data)
