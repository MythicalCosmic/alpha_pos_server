from base.requests.base import validate_request


def create_category_request(request):
    return validate_request(request, ['name'])


def update_status_request(request):
    return validate_request(request, ['status'])


def reorder_request(request):
    return validate_request(request, ['orders'])


def bulk_ids_request(request):
    return validate_request(request, ['ids'])
