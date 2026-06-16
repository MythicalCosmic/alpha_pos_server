"""Courier app + auth routes (root-level paths the mobile app calls, spec §3).
Mounted at '' in config/urls.py."""
from django.urls import path

from couriers import views

urlpatterns = [
    # auth
    path('auth/courier/login/', views.courier_login, name='courier-login'),

    # profile + feeds
    path('courier/me/', views.me, name='courier-me'),
    path('courier/orders/active/', views.orders_active, name='courier-orders-active'),
    path('courier/orders/completed/', views.orders_completed, name='courier-orders-completed'),
    path('courier/stats/today/', views.stats_today, name='courier-stats-today'),
    path('courier/balance/', views.balance, name='courier-balance'),
    path('courier/notifications/', views.notifications, name='courier-notifications'),
    path('courier/shift/reconciliation/', views.shift_reconciliation, name='courier-reconciliation'),

    # shift / location / push
    path('courier/location/', views.location, name='courier-location'),
    path('courier/shift/online/', views.shift_online, name='courier-shift-online'),
    path('courier/shift/settle/', views.shift_settle, name='courier-shift-settle'),
    path('courier/push-token/', views.push_token, name='courier-push-token'),

    # order actions
    path('orders/<int:order_id>/accept/', views.order_accept, name='courier-order-accept'),
    path('orders/<int:order_id>/decline/', views.order_decline, name='courier-order-decline'),
    path('orders/<int:order_id>/status/', views.order_status, name='courier-order-status'),
]
