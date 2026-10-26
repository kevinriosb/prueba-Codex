"""Autoriza o retira una cuenta en la lista privada; no borra datos ni usa APIs."""
import argparse
from pathlib import Path
import re
import sys

if __package__:
    from .configure_hostinger_telegram import render_variables, variable
    from .update_openai_key import read_private, write_updated
else:
    from configure_hostinger_telegram import render_variables, variable
    from update_openai_key import read_private, write_updated


ROOT=Path(__file__).resolve().parents[1]


def change(original, identifier, *, remove=False):
    if not re.fullmatch(r'[1-9][0-9]{0,15}',identifier) or int(identifier)>=2**52:
        raise ValueError('Introduce el ID numérico positivo de la cuenta; no su @usuario.')
    current=variable(original,'TELEGRAM_ALLOWED_USER_IDS')
    parts=[value.strip() for value in current.split(',')]
    if not parts or any(not re.fullmatch(r'[1-9][0-9]{0,15}',value) or int(value)>=2**52 for value in parts):
        raise ValueError('Primero completa la conexión inicial de Telegram.')
    users=set(parts)
    if remove:
        users.discard(identifier)
        if not users:
            raise ValueError('No se retira la última cuenta desde este asistente. Conserva al menos una cuenta autorizada.')
    else:
        users.add(identifier)
    if len(users)>100:
        raise ValueError('La instalación privada admite como máximo 100 cuentas autorizadas.')
    return render_variables(original,{'TELEGRAM_ALLOWED_USER_IDS':','.join(sorted(users,key=int))}),len(users)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('add','remove'))
    args=parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Ejecuta el asistente directamente en Termius.')
    path=ROOT/'.env.hostinger'
    original,_=read_private(path)
    identifier=input('ID numérico de la cuenta que has verificado (Enter cancela): ').strip()
    if not identifier:
        raise ValueError('Cancelado. No se modificó la lista.')
    updated,count=change(original,identifier,remove=args.operation=='remove')
    backup=write_updated(path,updated,original,label='telegram-users')
    if backup:
        print('Lista actualizada. Copia privada anterior: '+backup.name)
    else:
        print('La lista ya tenía ese estado. No se cambió ningún archivo.')
    print('Cuentas autorizadas: '+str(count)+'. Los perfiles, documentos y conversaciones se conservan.')
    print('Recrea solo agent para aplicar la lista. No se hicieron llamadas a APIs ni se reinició el servicio.')
    return 0


def cli():
    try:
        return main()
    except ValueError as error:
        print(str(error))
    except (EOFError,KeyboardInterrupt):
        print('\nCambio interrumpido. No se muestran valores privados.')
    except Exception:
        print('No se pudo actualizar la lista. Conserva los archivos privados; no compartas su contenido.')
    return 1


if __name__=='__main__':
    raise SystemExit(cli())
