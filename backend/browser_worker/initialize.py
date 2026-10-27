"""Genera una credencial interna una sola vez, sin mostrarla ni reemplazarla."""
import os
import secrets
from pathlib import Path


def main():
    path=Path('/run/browser-auth/token')
    try:
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or len(path.read_text().strip())<40:
            raise RuntimeError('La credencial interna existente no es válida.') from None
    else:
        with os.fdopen(descriptor,'w') as stream:
            stream.write(secrets.token_urlsafe(48)+'\n')
    print('Credencial interna disponible. No se modificaron datos fiscales.')


if __name__=='__main__':
    main()
