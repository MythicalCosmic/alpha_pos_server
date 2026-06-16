"""Back-office courier routes, mounted under /api/admins/couriers/ in config/urls.py."""
from django.urls import path

from couriers import admin_views

urlpatterns = [
    path('', admin_views.couriers_list, name='admin-couriers-list'),
    path('assign', admin_views.assign_order, name='admin-couriers-assign'),
]
