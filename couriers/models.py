"""Courier delivery models (server edition only).

These orchestrate the *delivery handoff* on top of the synced POS order:

  base.Order  ── the order itself (syncs to/from tills via SyncMixin)
  Courier     ── a delivery rider (linked to a staff base.User)
  DeliveryAssignment ── links one Order to one Courier + the courier `step`
                        projection (ASSIGNED→READY→PICKED_UP→ON_WAY→DELIVERED)
  LocationPing ── last-known GPS for the courier (relayed to the cashier desktop)
  PushToken    ── Expo/FCM token for background push

Deliberately NOT SyncMixin models: the courier layer is server-side dispatch
state, it must not sync down to every till. The *order* already syncs; the
courier app talks to the server directly over REST + WebSocket. Money is
integer so'm (BigIntegerField), never floats.
"""
from django.db import models


class Courier(models.Model):
    """A delivery rider. Authenticates as a staff `base.User`; the courier
    profile carries the rider-specific fields the mobile app renders."""

    user = models.OneToOneField(
        'base.User', on_delete=models.CASCADE, related_name='courier',
    )
    # Profile (the app reads these; first/last fall back to the User's names).
    first_name = models.CharField(max_length=50, blank=True, default='')
    last_name = models.CharField(max_length=50, blank=True, default='')
    phone = models.CharField(max_length=24, db_index=True)
    vehicle = models.CharField(max_length=32, blank=True, default='Scooter')
    plate = models.CharField(max_length=24, blank=True, default='')
    # The app's `courier.id` (e.g. "CR-118"). Stable, human-facing, unique.
    code = models.CharField(max_length=16, unique=True)
    # Branch is a string id across this system (SyncMixin.branch_id), not a FK.
    branch_id = models.CharField(max_length=50, blank=True, default='', db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default='')
    rating = models.DecimalField(max_digits=2, decimal_places=1, default=5.0)
    online = models.BooleanField(default=False)      # on-shift toggle
    share_loc = models.BooleanField(default=True)     # "share live location"
    shift_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} ({self.full_name})'

    @property
    def full_name(self):
        first = self.first_name or getattr(self.user, 'first_name', '')
        last = self.last_name or getattr(self.user, 'last_name', '')
        return f'{first} {last}'.strip()

    def current_delivery(self):
        """The assignment the courier is actively delivering (PICKED_UP/ON_WAY),
        used to scope location relay to the order's cashier. None when idle."""
        return (self.assignments
                .filter(step__in=(DeliveryAssignment.Step.PICKED_UP,
                                  DeliveryAssignment.Step.ON_WAY))
                .select_related('order')
                .order_by('-assigned_at')
                .first())


class DeliveryAssignment(models.Model):
    """Links a delivery Order to a Courier and tracks the courier `step`
    projection. The kitchen/order status (PREPARING/READY) stays on base.Order;
    this `step` is the courier-facing view of the same lifecycle."""

    class Step(models.TextChoices):
        ASSIGNED = 'ASSIGNED', 'Assigned (kitchen preparing)'
        READY = 'READY', 'Ready for pickup'
        PICKED_UP = 'PICKED_UP', 'Picked up'
        ON_WAY = 'ON_WAY', 'On the way'
        DELIVERED = 'DELIVERED', 'Delivered'
        DECLINED = 'DECLINED', 'Declined / unassigned'

    # Forward-only order; the courier may only advance, never go back.
    FORWARD = ['ASSIGNED', 'READY', 'PICKED_UP', 'ON_WAY', 'DELIVERED']
    # Steps the courier is allowed to set via POST /orders/<id>/status/.
    COURIER_SETTABLE = {'PICKED_UP', 'ON_WAY', 'DELIVERED'}

    order = models.OneToOneField(
        'base.Order', on_delete=models.CASCADE, related_name='courier_delivery',
    )
    courier = models.ForeignKey(
        Courier, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='assignments',
    )
    step = models.CharField(
        max_length=12, choices=Step.choices, default=Step.ASSIGNED, db_index=True,
    )
    fee = models.BigIntegerField(default=0)          # courier delivery fee, so'm

    assigned_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    picked_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # Hold-to-accept window: the IncomingOrderSheet countdown. Past this, the
    # accept is rejected and the order can be reassigned.
    expires_at = models.DateTimeField(null=True, blank=True)
    declined_reason = models.CharField(max_length=200, blank=True, default='')

    # Address snapshot (text-only OR with coords — the app shows a map pin only
    # when lat/lng are present).
    addr_text = models.CharField(max_length=255, blank=True, default='')
    addr_landmark = models.CharField(max_length=255, blank=True, default='')
    addr_lat = models.FloatField(null=True, blank=True)
    addr_lng = models.FloatField(null=True, blank=True)
    distance_km = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-assigned_at']
        indexes = [models.Index(fields=['courier', 'step'])]

    def __str__(self):
        return f'order={self.order_id} courier={self.courier_id} step={self.step}'

    def can_advance_to(self, target):
        """Forward-only along FORWARD. Returns True if `target` is strictly
        ahead of the current step."""
        try:
            return self.FORWARD.index(target) > self.FORWARD.index(self.step)
        except ValueError:
            return False


class LocationPing(models.Model):
    """Last-known courier position. Upserted (one row per courier) — we only
    need the latest for the desktop map, not a GPS trail."""

    courier = models.OneToOneField(
        Courier, on_delete=models.CASCADE, related_name='location',
    )
    lat = models.FloatField()
    lng = models.FloatField()
    at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.courier_id} @ {self.lat},{self.lng}'


class PushToken(models.Model):
    """Expo push token (or FCM) for background notifications."""

    courier = models.ForeignKey(
        Courier, on_delete=models.CASCADE, related_name='push_tokens',
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=8, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.courier_id}:{self.platform}'


class CourierPayment(models.Model):
    """A payment the courier collects against a delivery order.

    Record-only (see ``couriers.payments``): there is no live gateway, so the
    courier confirms the method at the door and the row lands ``PAID`` — exactly
    like recording cash. ``status`` + ``external_id`` exist so a future gateway
    webhook can drive a ``PENDING -> PAID`` flow without a schema change. Money
    is integer so'm. This is courier-layer state — NOT a synced ``OrderPayment``
    row (the cloud sets the Order's rolled-up paid fields instead, which is what
    the shift/AI reports read)."""

    class Provider(models.TextChoices):
        CASH = 'CASH', 'Cash'
        CARD = 'CARD', 'Card (terminal)'
        QR = 'QR', 'QR / online'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PAID = 'PAID', 'Paid'
        REFUNDED = 'REFUNDED', 'Refunded'
        FAILED = 'FAILED', 'Failed'

    # Maps the courier-facing provider onto the POS Order.PaymentMethod so the
    # order's rolled-up payment_method stays consistent with the rest of the POS.
    PROVIDER_TO_METHOD = {
        Provider.CASH: 'CASH',
        Provider.CARD: 'UZCARD',
        Provider.QR: 'PAYME',
    }

    order = models.ForeignKey(
        'base.Order', on_delete=models.CASCADE, related_name='courier_payments',
    )
    courier = models.ForeignKey(
        Courier, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='payments',
    )
    provider = models.CharField(max_length=8, choices=Provider.choices)
    amount = models.BigIntegerField()                       # so'm
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    link = models.CharField(max_length=512, blank=True, default='')   # gateway pay link (QR/online)
    external_id = models.CharField(            # gateway ref / idempotency key
        max_length=128,
    )
    branch_id = models.CharField(max_length=50, blank=True, default='', db_index=True)
    note = models.CharField(max_length=200, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['courier', 'status']),
            models.Index(fields=['order', 'status']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['external_id'],
                name='uniq_courier_payment_external_id',
            ),
            models.CheckConstraint(
                condition=~models.Q(external_id=''),
                name='courier_payment_external_id_required',
            ),
        ]

    def __str__(self):
        return f'CourierPayment<{self.provider} {self.amount} #{self.order_id} {self.status}>'

    @property
    def is_cash(self):
        return self.provider == self.Provider.CASH


class CourierSettlement(models.Model):
    """A shift-handover snapshot. Closing one is what "settles" the courier:
    every payment, refund, and delivery in the half-open window
    ``[period_start, period_end)`` is considered settled. The numbers are a
    frozen copy of the reconciliation at handover time (so'm)."""

    courier = models.ForeignKey(
        Courier, on_delete=models.CASCADE, related_name='settlements',
    )
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(db_index=True)

    deliveries = models.IntegerField(default=0)
    cash_collected = models.BigIntegerField(default=0)
    qr_collected = models.BigIntegerField(default=0)     # card + QR (non-cash)
    delivery_fees = models.BigIntegerField(default=0)
    bonuses = models.BigIntegerField(default=0)
    tips = models.BigIntegerField(default=0)
    net_payout = models.BigIntegerField(default=0)       # fees + bonuses + tips
    handover_code = models.CharField(max_length=24, blank=True, default='')
    note = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-at']
        indexes = [models.Index(fields=['courier', 'period_end'])]

    def __str__(self):
        return f'Settlement<{self.courier_id} @ {self.at:%Y-%m-%d %H:%M}>'


class CourierNotification(models.Model):
    """A persisted notification for the courier app's bell feed. Written when an
    order is assigned / ready / paid; the app marks them read. Shapes mirror the
    feed the app already renders (icon/tone are display tokens)."""

    courier = models.ForeignKey(
        Courier, on_delete=models.CASCADE, related_name='notifications',
    )
    icon = models.CharField(max_length=24, blank=True, default='bell')
    tone = models.CharField(max_length=12, blank=True, default='primary')
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=400, blank=True, default='')
    order = models.ForeignKey(
        'base.Order', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['courier', 'read_at'])]

    def __str__(self):
        return f'Notif<{self.courier_id} {self.title!r}>'


class LocationTrailPoint(models.Model):
    """An appended GPS breadcrumb (unlike ``LocationPing`` which keeps only the
    latest). Used to compute the courier's distanceKm over a shift. Recorded
    only while on-shift + sharing location; pruned by age at end of shift
    (services.set_online) and by the ``prune_courier_trail`` management command."""

    courier = models.ForeignKey(
        Courier, on_delete=models.CASCADE, related_name='trail',
    )
    lat = models.FloatField()
    lng = models.FloatField()
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['at']
        indexes = [models.Index(fields=['courier', 'at'])]

    def __str__(self):
        return f'{self.courier_id} trail @ {self.lat},{self.lng}'
