import json
from typing import Protocol
from urllib.parse import urlsplit

from ..errors import AppError
from .models import RFC
from .providers import normalized_email


class PortalAdapter(Protocol):
    """Solo se registran adaptadores verificados desde código/configuración de servidor."""
    name: str
    authorized: bool

    def supports(self,ticket:dict)->bool: ...


def official_contacts(encoded):
    try:
        entries=json.loads(encoded)
        if not isinstance(entries,list) or len(entries)>50:
            raise ValueError()
        result={}
        for entry in entries:
            if not isinstance(entry,dict) or entry.get('verified') is not True or not RFC.fullmatch(entry.get('issuer_rfc','')):
                raise ValueError()
            uri=urlsplit(entry['official_url'])
            if uri.scheme!='https' or not uri.hostname or uri.username or uri.password or len(entry['official_url'])>500:
                raise ValueError()
            result[entry['issuer_rfc']]={'email':normalized_email(entry['email']),'official_url':entry['official_url']}
        return result
    except (ValueError,KeyError,TypeError,AppError):
        raise AppError(503,'official_contacts_configuration','Revisa el catálogo de contactos fiscales verificados del servidor.') from None
