"""Public immutable delivery for operator-uploaded catalog images."""
from django.http import FileResponse, Http404
from django.urls import path
from django.views.decorators.http import require_GET

from smartfood.services.media_service import media_file


def _serve(kind, filename):
    result = media_file(kind, filename)
    if result is None:
        raise Http404('Image not found')
    file_handle, content_type = result
    response = FileResponse(file_handle, content_type=content_type)
    response['Cache-Control'] = 'public, max-age=31536000, immutable'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_GET
def product_image(request, filename):
    return _serve('products', filename)


@require_GET
def banner_image(request, filename):
    return _serve('banners', filename)


@require_GET
def reward_image(request, filename):
    return _serve('rewards', filename)


urlpatterns = [
    path('media/products/<str:filename>', product_image, name='smartfood-product-image'),
    path('media/banners/<str:filename>', banner_image, name='smartfood-banner-image'),
    path('media/rewards/<str:filename>', reward_image, name='smartfood-reward-image'),
]
