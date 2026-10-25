import io
import warnings

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from ..errors import AppError


def inspect_document(data, maximum, *, allow_xml=False):
    if not data or len(data)>maximum:
        raise AppError(413, 'document_size', 'El documento está vacío o supera el límite permitido.')
    if allow_xml and data.lstrip().startswith(b'<') and not data.startswith(b'%PDF'):
        if len(data)>1_048_576:
            raise AppError(413, 'xml_size', 'El XML supera 1 MB.')
        return 'application/xml', data
    if data.startswith(b'%PDF-'):
        try:
            pdf = PdfReader(io.BytesIO(data),strict=True)
            if pdf.is_encrypted or not 1<=len(pdf.pages)<=5:
                raise ValueError()
            for marker in (b'/JavaScript',b'/JS',b'/Launch',b'/EmbeddedFile',b'/XFA'):
                if marker in data:
                    raise ValueError()
        except Exception:
            raise AppError(422,'invalid_pdf','Usa un PDF legible, sin contraseña, de hasta cinco páginas.') from None
        return 'application/pdf',data
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'JPEG','PNG','WEBP'} or image.width*image.height>16_000_000:
                    raise ValueError()
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image = image.convert('RGB')
                image.thumbnail((1600,1600))
                output=io.BytesIO()
                image.save(output,format='JPEG',quality=90)
        return 'image/jpeg',output.getvalue()
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombWarning,Image.DecompressionBombError):
        raise AppError(422,'invalid_document','Usa una fotografía JPEG/PNG/WebP o un PDF válido.') from None
