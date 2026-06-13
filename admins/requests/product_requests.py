from base.requests.base import validate_request


def create_product_request(request):
    return validate_request(request, ['name', 'price', 'category_id'])


def bulk_ids_request(request):
    return validate_request(request, ['ids'])
