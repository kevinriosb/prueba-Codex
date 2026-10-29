"""Publica fallos de pytest como anotaciones; no necesita descargar los logs."""
import sys
from pathlib import Path
from xml.etree import ElementTree


def escaped(value):
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def main(path):
    if not path.is_file():
        print('::notice::No se generó el informe de pytest; revisa el paso que falló antes de las pruebas.')
        return
    root = ElementTree.parse(path).getroot()
    count = 0
    for case in root.iter('testcase'):
        for tag in ('failure', 'error'):
            for failure in case.findall(tag):
                title = case.get('classname', '') + '::' + case.get('name', 'pytest')
                title = escaped(title).replace(',', '%2C').replace(':', '%3A')
                detail = failure.text or failure.get('message', 'Fallo sin detalle.')
                print('::error title=' + title + '::' + escaped(detail[-6000:]))
                count += 1
                if count >= 10:
                    return


if __name__ == '__main__':
    main(Path(sys.argv[1]))
