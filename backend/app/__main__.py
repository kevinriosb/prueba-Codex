import logging

import uvicorn

from .main import create_app
from .config import Settings


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    logging.getLogger('openai').setLevel(logging.WARNING)
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
        access_log=False, timeout_keep_alive=5, limit_concurrency=50, workers=1,
        proxy_headers=True, forwarded_allow_ips=settings.forwarded_allow_ips)


if __name__ == '__main__':
    main()
