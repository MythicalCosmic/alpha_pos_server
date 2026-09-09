"""Response helpers shared by browser-download endpoints."""
from django.http import HttpResponse
from django.utils.http import content_disposition_header

from admins.services.workbook_export_service import XLSX_CONTENT_TYPE


def xlsx_attachment(payload, filename, *, count=0):
    response = HttpResponse(payload, content_type=XLSX_CONTENT_TYPE)
    response['Content-Disposition'] = content_disposition_header(
        True, filename,
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Export-Count'] = str(count)
    # The admin panel may be hosted separately from the API.  Let its browser
    # read the server-selected filename and count instead of hard-coding them.
    response['Access-Control-Expose-Headers'] = (
        'Content-Disposition, X-Export-Count'
    )
    return response


def file_attachment(payload, filename, content_type, *, count=0, headers=None):
    """Safe browser attachment response for non-XLSX and report exports."""
    response = HttpResponse(payload, content_type=content_type)
    response['Content-Disposition'] = content_disposition_header(True, filename)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Export-Count'] = str(count)
    exposed = ['Content-Disposition', 'X-Export-Count']
    for name, value in (headers or {}).items():
        response[name] = str(value)
        exposed.append(name)
    response['Access-Control-Expose-Headers'] = ', '.join(exposed)
    return response
