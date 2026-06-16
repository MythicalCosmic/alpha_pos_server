from django.contrib import admin

from couriers.models import Courier, DeliveryAssignment, LocationPing, PushToken


@admin.register(Courier)
class CourierAdmin(admin.ModelAdmin):
    list_display = ('code', 'full_name', 'phone', 'vehicle', 'online', 'rating', 'branch_id')
    search_fields = ('code', 'phone', 'first_name', 'last_name', 'user__email')
    list_filter = ('online', 'share_loc', 'branch_id')


@admin.register(DeliveryAssignment)
class DeliveryAssignmentAdmin(admin.ModelAdmin):
    list_display = ('order_id', 'courier', 'step', 'fee', 'assigned_at', 'delivered_at')
    list_filter = ('step',)
    search_fields = ('order__id', 'courier__code')
    raw_id_fields = ('order', 'courier')


@admin.register(LocationPing)
class LocationPingAdmin(admin.ModelAdmin):
    list_display = ('courier', 'lat', 'lng', 'at')


@admin.register(PushToken)
class PushTokenAdmin(admin.ModelAdmin):
    list_display = ('courier', 'platform', 'created_at')
    search_fields = ('courier__code', 'token')
