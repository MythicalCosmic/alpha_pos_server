from django.contrib import admin

from couriers.models import (
    Courier, DeliveryAssignment, LocationPing, PushToken,
    CourierPayment, CourierSettlement, CourierNotification, LocationTrailPoint,
)


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


@admin.register(CourierPayment)
class CourierPaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'order_id', 'courier', 'provider', 'amount', 'status',
                    'created_at', 'paid_at', 'refunded_at')
    list_filter = ('provider', 'status', 'branch_id')
    search_fields = ('order__id', 'courier__code', 'external_id')
    raw_id_fields = ('order', 'courier')


@admin.register(CourierSettlement)
class CourierSettlementAdmin(admin.ModelAdmin):
    list_display = ('id', 'courier', 'at', 'deliveries', 'cash_collected',
                    'qr_collected', 'delivery_fees', 'net_payout')
    list_filter = ('courier',)
    search_fields = ('courier__code', 'handover_code')


@admin.register(CourierNotification)
class CourierNotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'courier', 'title', 'tone', 'read_at', 'created_at')
    list_filter = ('tone',)
    search_fields = ('courier__code', 'title', 'body')
    raw_id_fields = ('order',)


@admin.register(LocationTrailPoint)
class LocationTrailPointAdmin(admin.ModelAdmin):
    list_display = ('courier', 'lat', 'lng', 'at')
    list_filter = ('courier',)
