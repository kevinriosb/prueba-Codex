"""Contabilidad de página y caché HTTP efímera, privada de cada contexto."""
import re
import time
from collections import OrderedDict
from dataclasses import dataclass,field


@dataclass
class PageResources:
    requests:int=0
    size:int=0
    issues:list=field(default_factory=list)


class ResourceCache:
    def __init__(self,maximum=8_388_608,ttl=180):
        self.maximum,self.ttl=maximum,ttl
        self.entries=OrderedDict()
        self.size=0

    def get(self,url,headers):
        entry=self.entries.get(url)
        if not entry:
            return None
        if entry['expires']<=time.monotonic():
            self._remove(url)
            return None
        if entry['cookie']!=headers.get('cookie','') or any(headers.get(name,'')!=value for name,value in entry['vary'].items()):
            return None
        self.entries.move_to_end(url)
        return entry['response']

    def put(self,url,request,status,headers,body):
        control=headers.get('cache-control','').casefold()
        age=re.search(r'(?:^|,)\s*max-age\s*=\s*(?:"([0-9]+)"|([0-9]+))\s*(?:,|$)',control)
        vary={name.strip().lower() for name in headers.get('vary','').split(',') if name.strip()}
        if (status!=200 or not age or re.search(r'(?:^|,)\s*(?:private|no-store|no-cache)\b',control)
                or 'set-cookie' in headers or '*' in vary or len(body)>self.maximum):
            return
        if not vary<={'accept','accept-language','accept-encoding','origin','referer','user-agent','cookie'}:
            return
        digits=(age.group(1) or age.group(2)).lstrip('0') or '0'
        seconds=self.ttl if len(digits)>9 else min(int(digits),self.ttl)
        if seconds<=0:
            return
        self._remove(url)
        while self.entries and self.size+len(body)>self.maximum:
            self._remove(next(iter(self.entries)))
        self.entries[url]={'expires':time.monotonic()+seconds,'cookie':request.get('cookie',''),
            'vary':{name:request.get(name,'') for name in vary},'response':(status,dict(headers),body)}
        self.size+=len(body)

    def _remove(self,url):
        previous=self.entries.pop(url,None)
        if previous:
            self.size-=len(previous['response'][2])

    def clear(self):
        self.entries.clear()
        self.size=0
