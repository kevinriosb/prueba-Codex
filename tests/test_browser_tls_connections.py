"""Cada hostname debe recibir su propia validación TLS aun si comparte IP."""
import ssl

from httpcore2._backends.mock import AsyncMockStream,AsyncMockBackend

from backend.browser_worker.policy import PublicFetcher


async def test_two_hosts_on_same_pinned_ip_each_get_their_own_tls_handshake():
    names=[];connections=[]
    class Stream(AsyncMockStream):
        async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
            assert ssl_context.check_hostname and ssl_context.verify_mode==ssl.CERT_REQUIRED
            names.append(server_hostname)
            return self
    class Backend(AsyncMockBackend):
        async def connect_tcp(self,host,port,**kwargs):
            connections.append((host,port))
            return Stream([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK',
                b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK'])
    async def resolver(_):return ['93.184.216.34']
    fetcher=PublicFetcher(resolver=resolver)
    fetcher.client._transport._pool._network_backend=Backend([])
    try:
        await fetcher.fetch('https://first.example/',{})
        await fetcher.fetch('https://second.example/',{})
        assert names==['first.example','second.example']
        assert connections==[('93.184.216.34',443)]*2
    finally:await fetcher.close()
