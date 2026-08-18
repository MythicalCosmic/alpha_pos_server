"""Public immutable delivery for operator-uploaded catalog images."""
from django.http import FileResponse, Http404
from django.urls import path
from django.views.decorators.http import require_GET

from smartfood.services.media_service import product_media_file


@require_GET
def product_image(request, filename):
    result = product_media_file(filename)
    if result is None:
        raise Http404('Image not found')
    file_handle, content_type = result
    response = FileResponse(file_handle, content_type=content_type)
    response['Cache-Control'] = 'public, max-age=31536000, immutable'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


urlpatterns = [
    path('media/products/<str:filename>', product_image, name='smartfood-product-image'),
]
