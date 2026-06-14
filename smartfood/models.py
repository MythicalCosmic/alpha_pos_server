"""Smart Food — customer-delivery data model (server edition only).

Design rules (see the build plan):
  * The catalog is DRIVEN BY the existing POS: base.Product / base.Category are
    never duplicated. A thin one-to-one "shadow" (BotProduct / BotCategory) adds
    bot-only state: published (accepted to the bot), is_selling (runtime
    stop-selling / out of stock), trilingual name/description overrides, image,
    and bot ordering. Product price is ALWAYS read live from base.Product.price
    (the cloud owns price), never stored here.
  * Sizes and toppings DO NOT exist in the POS — they are new here, attached to
    base.Product by FK (string ref, so no import cycle).
  * Customers are NOT staff base.User: a Telegram customer must never touch
    roles/permissions/shifts/password auth and must stay server-local, so we use
    a dedicated Customer + CustomerSession (sha256-token, mirroring base.Session).
  * NOTHING here uses SyncMixin: this is host-local customer data, never synced
    to branches.

A bot order is created PENDING; an operator dispatches it to a specific on-duty
cashier, which creates a real base.Order under that cashier (DispatchService).
"""
from django.db import models

LANG_CHOICES = (('uz', 'Uzbek'), ('ru', 'Russian'), ('en', 'English'))


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------- #
#  Runtime config (singleton)                                                  #
# --------------------------------------------------------------------------- #
class BotConfig(models.Model):
    """Singleton (pk=1) holding runtime bot state — mirrors base.AppSettings.

    `enabled` is the master ON/OFF: when False, customer endpoints return a
    "closed" payload and the bot stops offering the menu — flippable at runtime
    with no restart.
    """
    enabled = models.BooleanField(default=False)
    currency = models.CharField(max_length=8, default='UZS')
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    free_delivery_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    min_order_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    default_tip_options = models.JSONField(default=list, blank=True)
    service_area = models.JSONField(default=dict, blank=True)   # {city, center{lat,lng}, polygon[]}
    default_lang = models.CharField(max_length=2, choices=LANG_CHOICES, default='uz')
    loyalty_earn_per = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # 1 point per N UZS (0 = off)
    loyalty_point_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # 1 point = N UZS at redeem
    support_phone = models.CharField(max_length=32, blank=True, default='')
    support_telegram = models.CharField(max_length=64, blank=True, default='')
    support_email = models.CharField(max_length=120, blank=True, default='')
    support_chat_id = models.CharField(max_length=64, blank=True, default='')  # operator alert chat
    updated_at = models.DateTimeField(auto_now=True)

    _CACHE_KEY = 'smartfood:bot_config:v1'
    _CACHE_TTL = 60

    class Meta:
        verbose_name = 'bot config'
        verbose_name_plural = 'bot config'

    def save(self, *args, **kwargs):
        # Singleton: always row 1 (mirrors base.AppSettings — no CheckConstraint needed).
        self.pk = 1
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self._CACHE_KEY)

    @classmethod
    def load(cls):
        from django.core.cache import cache
        cached = cache.get(cls._CACHE_KEY)
        if cached is not None:
            return cached
        obj, _ = cls.objects.get_or_create(pk=1)
        cache.set(cls._CACHE_KEY, obj, cls._CACHE_TTL)
        return obj

    def __str__(self):
        return f"BotConfig(enabled={self.enabled})"


# --------------------------------------------------------------------------- #
#  Catalog shadow layer (publish / stop-selling over the POS catalog)         #
# --------------------------------------------------------------------------- #
class BotCategory(TimeStamped):
    """Bot-publishing shadow of a POS Category."""
    category = models.OneToOneField('base.Category', on_delete=models.CASCADE, related_name='bot')
    is_published = models.BooleanField(default=False)   # accepted to the bot
    is_selling = models.BooleanField(default=True)      # runtime stop-selling
    name_uz = models.CharField(max_length=80, blank=True, default='')
    name_ru = models.CharField(max_length=80, blank=True, default='')
    name_en = models.CharField(max_length=80, blank=True, default='')
    image_url = models.URLField(blank=True, default='')
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name_plural = 'bot categories'

    def __str__(self):
        return f"BotCategory({self.category_id}, pub={self.is_published})"


class BotProduct(TimeStamped):
    """Bot-publishing shadow of a POS Product. Price is read live from the POS."""
    product = models.OneToOneField('base.Product', on_delete=models.CASCADE, related_name='bot')
    is_published = models.BooleanField(default=False)
    is_selling = models.BooleanField(default=True)
    name_uz = models.CharField(max_length=120, blank=True, default='')
    name_ru = models.CharField(max_length=120, blank=True, default='')
    name_en = models.CharField(max_length=120, blank=True, default='')
    desc_uz = models.TextField(blank=True, default='')
    desc_ru = models.TextField(blank=True, default='')
    desc_en = models.TextField(blank=True, default='')
    image_url = models.URLField(blank=True, default='')
    tag = models.CharField(max_length=20, blank=True, default='')   # bestseller|new|spicy|''
    kcal = models.PositiveIntegerField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"BotProduct({self.product_id}, pub={self.is_published})"


class Size(TimeStamped):
    """Selectable size tier for a product (NEW — POS has no sizes)."""
    product = models.ForeignKey('base.Product', on_delete=models.CASCADE, related_name='bot_sizes')
    name_uz = models.CharField(max_length=40, blank=True, default='')
    name_ru = models.CharField(max_length=40, blank=True, default='')
    name_en = models.CharField(max_length=40, blank=True, default='')
    price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # added to base price
    is_default = models.BooleanField(default=False)
    is_selling = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"Size({self.product_id}, +{self.price_delta})"


class ToppingGroup(TimeStamped):
    """An option set on a product (e.g. "Sauces"), with required/min/max rules."""
    product = models.ForeignKey('base.Product', on_delete=models.CASCADE, related_name='topping_groups')
    name_uz = models.CharField(max_length=60, blank=True, default='')
    name_ru = models.CharField(max_length=60, blank=True, default='')
    name_en = models.CharField(max_length=60, blank=True, default='')
    is_required = models.BooleanField(default=False)
    min_select = models.PositiveIntegerField(default=0)
    max_select = models.PositiveIntegerField(default=0)  # 0 = unlimited
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"ToppingGroup({self.product_id})"


class Topping(TimeStamped):
    """An option within a ToppingGroup."""
    group = models.ForeignKey(ToppingGroup, on_delete=models.CASCADE, related_name='toppings')
    name_uz = models.CharField(max_length=60, blank=True, default='')
    name_ru = models.CharField(max_length=60, blank=True, default='')
    name_en = models.CharField(max_length=60, blank=True, default='')
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_selling = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"Topping({self.name_uz or self.name_en}, {self.price})"


# --------------------------------------------------------------------------- #
#  Customer identity + session                                                 #
# --------------------------------------------------------------------------- #
class Customer(TimeStamped):
    """A Telegram customer (NOT a staff base.User)."""
    telegram_id = models.BigIntegerField(unique=True, db_index=True)
    username = models.CharField(max_length=64, blank=True, default='')
    first_name = models.CharField(max_length=64, blank=True, default='')
    last_name = models.CharField(max_length=64, blank=True, default='')
    phone_number = models.CharField(max_length=20, blank=True, default='')
    language = models.CharField(max_length=2, choices=LANG_CHOICES, default='uz')
    photo_url = models.URLField(blank=True, default='')
    loyalty_points = models.IntegerField(default=0)
    is_blocked = models.BooleanField(default=False)

    @property
    def name(self):
        return (f"{self.first_name} {self.last_name}").strip() or self.username or str(self.telegram_id)

    def __str__(self):
        return f"Customer({self.telegram_id}, {self.name})"


class CustomerSession(models.Model):
    """Bearer session for a customer — mirrors base.Session: only the SHA-256
    digest of the token is stored (raw token lives on the client)."""
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, db_index=True, related_name='sessions')
    payload = models.CharField(max_length=128, db_index=True)   # sha256(token) hexdigest
    user_agent = models.CharField(max_length=256, blank=True, default='')
    ip_address = models.CharField(max_length=45, blank=True, default='')
    last_activity = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self):
        if self.expires_at is None:
            return True
        from django.utils import timezone
        return self.expires_at <= timezone.now()

    def __str__(self):
        return f"CustomerSession(customer={self.customer_id})"


# --------------------------------------------------------------------------- #
#  Addresses                                                                   #
# --------------------------------------------------------------------------- #
class Address(TimeStamped):
    """A customer delivery address (base.Order has no address column)."""
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='addresses')
    label = models.CharField(max_length=40, blank=True, default='')   # Home / Work / ...
    line = models.TextField()                                         # full display text
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    city = models.CharField(max_length=80, blank=True, default='')
    street = models.CharField(max_length=120, blank=True, default='')
    house = models.CharField(max_length=40, blank=True, default='')
    apartment = models.CharField(max_length=40, blank=True, default='')
    entrance = models.CharField(max_length=40, blank=True, default='')
    floor = models.CharField(max_length=40, blank=True, default='')
    intercom = models.CharField(max_length=40, blank=True, default='')
    comment = models.TextField(blank=True, default='')
    precision = models.CharField(max_length=16, blank=True, default='')   # Yandex precision
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['-is_default', '-id']

    def __str__(self):
        return f"Address({self.customer_id}, {self.label or self.line[:20]})"


# --------------------------------------------------------------------------- #
#  Orders                                                                      #
# --------------------------------------------------------------------------- #
class BotOrder(TimeStamped):
    """A bot order — created PENDING, then DISPATCHED to a cashier (which mints
    a real base.Order under that cashier and links it via pos_order)."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending dispatch'
        DISPATCHED = 'DISPATCHED', 'Dispatched to cashier'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELED = 'CANCELED', 'Canceled'

    class OrderType(models.TextChoices):
        DELIVERY = 'DELIVERY', 'Delivery'
        PICKUP = 'PICKUP', 'Pickup'

    class Payment(models.TextChoices):
        CASH = 'CASH', 'Cash'
        CARD = 'CARD', 'Card'

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='orders')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    order_type = models.CharField(max_length=10, choices=OrderType.choices, default=OrderType.DELIVERY)
    address = models.ForeignKey(Address, on_delete=models.SET_NULL, null=True, blank=True)
    address_text = models.TextField(blank=True, default='')   # frozen snapshot of the address at order time
    phone_number = models.CharField(max_length=20, blank=True, default='')
    note = models.TextField(blank=True, default='')

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tip = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    loyalty_points_used = models.IntegerField(default=0)
    loyalty_points_earned = models.IntegerField(default=0)
    payment_method = models.CharField(max_length=8, choices=Payment.choices, default=Payment.CASH)

    # Dispatch linkage
    pos_order = models.OneToOneField('base.Order', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='bot_order')
    dispatched_cashier = models.ForeignKey('base.User', on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name='dispatched_bot_orders')
    dispatched_by = models.ForeignKey('base.User', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['status', 'created_at'])]

    @property
    def code(self):
        return f"SF-{self.id}"

    def __str__(self):
        return f"BotOrder({self.code}, {self.status})"


class BotOrderItem(TimeStamped):
    """A snapshot line — product + size + chosen toppings with FROZEN prices."""
    bot_order = models.ForeignKey(BotOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('base.Product', on_delete=models.PROTECT)
    size = models.ForeignKey(Size, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)   # base + size delta + toppings, frozen
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    toppings_snapshot = models.JSONField(default=list, blank=True)   # [{topping_id,name,price}]
    detail = models.TextField(blank=True, default='')               # becomes base.OrderItem.detail on dispatch

    def __str__(self):
        return f"BotOrderItem({self.product_id} x{self.quantity})"


# --------------------------------------------------------------------------- #
#  Support                                                                     #
# --------------------------------------------------------------------------- #
class SupportTicket(TimeStamped):
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        CLOSED = 'CLOSED', 'Closed'

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='tickets')
    subject = models.CharField(max_length=160, blank=True, default='')
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"SupportTicket({self.id}, {self.status})"


class SupportMessage(TimeStamped):
    class Sender(models.TextChoices):
        CUSTOMER = 'CUSTOMER', 'Customer'
        OPERATOR = 'OPERATOR', 'Operator'

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name='messages')
    sender = models.CharField(max_length=8, choices=Sender.choices, default=Sender.CUSTOMER)
    text = models.TextField()

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"SupportMessage(ticket={self.ticket_id}, {self.sender})"
