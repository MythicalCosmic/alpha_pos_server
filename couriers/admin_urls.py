"""Back-office courier routes, mounted under /api/admins/couriers/ in config/urls.py."""
from django.urls import path

from couriers import admin_views

urlpatterns = [
    # GET -> list, POST -> create (the FE calls POST /api/admins/couriers)
    path('', admin_views.couriers_root, name='admin-couriers-root'),
    path('create', admin_views.create_courier, name='admin-couriers-create'),
    path('<int:courier_id>/regenerate', admin_views.regenerate_credential,
         name='admin-couriers-regenerate'),
    path('assign', admin_views.assign_order, name='admin-couriers-assign'),
]
