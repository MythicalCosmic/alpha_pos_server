"""Django admin for the Smart Food customer bot — every bot model is registered
here so the whole storefront (open/close switch, published catalog, sizes,
toppings, customers, orders, support) is controllable from /admin/.

Catalog visibility (see smartfood/services/catalog_service.py): a product shows in
the bot only when its own BotProduct AND its category's BotCategory are BOTH
is_published=True and is_selling=True. Use the "Publish" / "Start selling"
actions below (or the inline checkboxes) to flip them.
"""
from django.contrib import admin

from .models import (
    BotConfig, BotCategory, BotProduct, Size, ToppingGroup, Topping,
    Customer, CustomerSession, Address, BotOrder, BotOrderItem,
    SupportTicket, SupportMessage,
)


# --------------------------------------------------------------------------- #
#  Shared bulk actions                                                         #
# --------------------------------------------------------------------------- #
@admin.action(description="Publish to bot (accept)")
def publish(modeladmin, request, queryset):
    queryset.update(is_published=True)


@admin.action(description="Unpublish from bot")
def unpublish(modeladmin, request, queryset):
    queryset.update(is_published=False)


@admin.action(description="Start selling (in stock)")
def start_selling(modeladmin, request, queryset):
    queryset.update(is_selling=True)


@admin.action(description="Stop selling (out of stock)")
def stop_selling(modeladmin, request, queryset):
    queryset.update(is_selling=False)


# --------------------------------------------------------------------------- #
#  Runtime config (singleton)                                                  #
# --------------------------------------------------------------------------- #
@admin.register(BotConfig)
class BotConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'enabled', 'currency', 'delivery_fee',
                    'min_order_amount', 'default_lang', 'updated_at')
    readonly_fields = ('updated_at',)
    fieldsets = (
        ('Master switch', {
            'fields': ('enabled',),
            'description': "Turn the whole customer bot ON/OFF. When OFF the Mini "
                           "App shows 'closed' (reason: bot_off).",
        }),
        ('Pricing & delivery', {
            'fields': ('currency', 'delivery_fee', 'free_delivery_threshold',
                       'min_order_amount', 'default_tip_options'),
        }),
        ('Service area & language', {
            'fields': ('service_area', 'default_lang'),
        }),
        ('Loyalty', {
            'fields': ('loyalty_earn_per', 'loyalty_point_value'),
        }),
        ('Support contacts', {
            'fields': ('support_phone', 'support_telegram', 'support_email',
                       'support_chat_id'),
        }),
        (None, {'fields': ('updated_at',)}),
    )

    # Singleton: exactly one row (pk=1). Forbid add when it exists and forbid delete.
    def has_add_permission(self, request):
        return not BotConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# --------------------------------------------------------------------------- #
#  Catalog shadow layer                                                        #
# --------------------------------------------------------------------------- #
@admin.register(BotCategory)
class BotCategoryAdmin(admin.ModelAdmin):
    list_display = ('category', 'is_published', 'is_selling', 'sort_order',
                    'name_uz', 'name_ru', 'name_en')
    list_editable = ('is_published', 'is_selling', 'sort_order')
    list_filter = ('is_published', 'is_selling')
    search_fields = ('category__name', 'name_uz', 'name_ru', 'name_en')
    autocomplete_fields = ('category',)
    actions = (publish, unpublish, start_selling, stop_selling)


@admin.register(BotProduct)
class BotProductAdmin(admin.ModelAdmin):
    list_display = ('product', 'category', 'is_published', 'is_selling', 'tag',
                    'sort_order')
    list_editable = ('is_published', 'is_selling', 'tag', 'sort_order')
    list_filter = ('is_published', 'is_selling', 'tag', 'product__category')
    search_fields = ('product__name', 'name_uz', 'name_ru', 'name_en')
    autocomplete_fields = ('product',)
    actions = (publish, unpublish, start_selling, stop_selling)

    @admin.display(description='Category', ordering='product__category')
    def category(self, obj):
        return obj.product.category if obj.product_id else None


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    list_display = ('product', 'name_uz', 'price_delta', 'is_default',
                    'is_selling', 'sort_order')
    list_editable = ('price_delta', 'is_default', 'is_selling', 'sort_order')
    list_filter = ('is_selling', 'is_default')
    search_fields = ('product__name', 'name_uz', 'name_ru', 'name_en')
    autocomplete_fields = ('product',)
    actions = (start_selling, stop_selling)


class ToppingInline(admin.TabularInline):
    model = Topping
    extra = 1


@admin.register(ToppingGroup)
class ToppingGroupAdmin(admin.ModelAdmin):
    list_display = ('product', 'name_uz', 'is_required', 'min_select',
                    'max_select', 'sort_order')
    list_editable = ('is_required', 'min_select', 'max_select', 'sort_order')
    search_fields = ('product__name', 'name_uz', 'name_ru', 'name_en')
    autocomplete_fields = ('product',)
    inlines = (ToppingInline,)


@admin.register(Topping)
class ToppingAdmin(admin.ModelAdmin):
    list_display = ('name_uz', 'group', 'price', 'is_selling', 'sort_order')
    list_editable = ('price', 'is_selling', 'sort_order')
    list_filter = ('is_selling',)
    search_fields = ('name_uz', 'name_ru', 'name_en', 'group__name_uz')
    actions = (start_selling, stop_selling)


# --------------------------------------------------------------------------- #
#  Customers, sessions, addresses                                              #
# --------------------------------------------------------------------------- #
@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('telegram_id', 'name', 'username', 'phone_number',
                    'language', 'loyalty_points', 'is_blocked', 'created_at')
    list_editable = ('loyalty_points', 'is_blocked')
    list_filter = ('is_blocked', 'language')
    search_fields = ('telegram_id', 'username', 'first_name', 'last_name',
                     'phone_number')


@admin.register(CustomerSession)
class CustomerSessionAdmin(admin.ModelAdmin):
    list_display = ('customer', 'last_activity', 'expires_at', 'ip_address')
    search_fields = ('customer__telegram_id', 'customer__username')
    readonly_fields = ('customer', 'payload', 'user_agent', 'ip_address',
                       'last_activity', 'expires_at', 'created_at')

    def has_add_permission(self, request):
        return False


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ('customer', 'label', 'line', 'city', 'is_default')
    list_filter = ('is_default', 'city')
    search_fields = ('customer__telegram_id', 'line', 'street', 'house')
    autocomplete_fields = ('customer',)


# --------------------------------------------------------------------------- #
#  Orders                                                                      #
# --------------------------------------------------------------------------- #
class BotOrderItemInline(admin.TabularInline):
    model = BotOrderItem
    extra = 0
    autocomplete_fields = ('product',)
    readonly_fields = ('unit_price', 'line_total', 'toppings_snapshot')


@admin.register(BotOrder)
class BotOrderAdmin(admin.ModelAdmin):
    list_display = ('code', 'customer', 'status', 'order_type', 'total',
                    'payment_method', 'dispatched_cashier', 'created_at')
    list_filter = ('status', 'order_type', 'payment_method')
    search_fields = ('id', 'customer__telegram_id', 'customer__phone_number',
                     'phone_number')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('customer', 'address', 'pos_order',
                           'dispatched_cashier', 'dispatched_by')
    inlines = (BotOrderItemInline,)


@admin.register(BotOrderItem)
class BotOrderItemAdmin(admin.ModelAdmin):
    list_display = ('bot_order', 'product', 'size', 'quantity', 'unit_price',
                    'line_total')
    search_fields = ('bot_order__id', 'product__name')
    autocomplete_fields = ('bot_order', 'product', 'size')


# --------------------------------------------------------------------------- #
#  Support                                                                     #
# --------------------------------------------------------------------------- #
class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 1


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'subject', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('id', 'customer__telegram_id', 'subject')
    autocomplete_fields = ('customer',)
    inlines = (SupportMessageInline,)


@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ('ticket', 'sender', 'text', 'created_at')
    list_filter = ('sender',)
    search_fields = ('ticket__id', 'text')
