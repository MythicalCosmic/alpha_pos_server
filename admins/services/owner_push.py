"""Owner-app push notifications: device registry, event outbox and delivery.

Events write ``OwnerPushOutbox`` rows (one per device) inside the transaction
that produced them; ``owner_push_worker`` delivers the rows through FCM with
retries. Nothing here raises into the caller: a push problem must never break
an expense, a shift or a sync.
"""
import logging
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from admins.models import AdminDevice, OwnerPushOutbox
from admins.services import fcm
from base.helpers.response import ServiceResponse
from base.models import User
from base.money import uzs_int

logger = logging.getLogger('admins.owner_push')

TZ = ZoneInfo('Asia/Tashkent')
KINDS = ('expense_pending', 'shift_closed', 'daily_summary')
LOCALES = ('uz', 'ru', 'en')
MAX_ATTEMPTS = 6
RECENT_DAYS = 2  # older events are backfills/imports and never notify

TEXT = {
    'expense_pending': {
        'uz': ('Tasdiqlash kerak', 'Yangi xarajat: {amount} so‘m — {category}{description}'),
        'ru': ('Нужно подтвердить', 'Новый расход: {amount} сум — {category}{description}'),
        'en': ('Approval needed', 'New expense: {amount} UZS — {category}{description}'),
    },
    'shift_closed': {
        'uz': ('Smena yopildi', '{cashier}: savdo {revenue} so‘m, {orders} ta buyurtma.'),
        'ru': ('Смена закрыта', '{cashier}: продажи {revenue} сум, {orders} заказов.'),
        'en': ('Shift closed', '{cashier}: sales {revenue} UZS, {orders} orders.'),
    },
    'daily_summary': {
        'uz': ('{day} kun yakuni', 'Savdo {sales} so‘m, foyda {profit} so‘m. Seyf {safe}, bank {bank}.'),
        'ru': ('Итоги {day}', 'Продажи {sales} сум, прибыль {profit} сум. Сейф {safe}, банк {bank}.'),
        'en': ('{day} summary', 'Sales {sales} UZS, profit {profit} UZS. Safe {safe}, bank {bank}.'),
    },
}


def money(value):
    return f'{uzs_int(value or 0):,}'


def enabled():
    return getattr(settings, 'OWNER_PUSH_ENABLED', True) and fcm.is_configured()


# ---------------------------------------------------------------- devices

def serialize_device(device):
    return {
        'id': device.id,
        'platform': device.platform,
        'app_version': device.app_version,
        'locale': device.locale,
        'prefs': {**AdminDevice.DEFAULT_PREFS, **(device.prefs or {})},
        'is_active': device.is_active,
        'last_seen_at': device.last_seen_at.isoformat() if device.last_seen_at else None,
    }


def _clean_prefs(value):
    if value is None:
        return {}, None
    if not isinstance(value, dict) or any(k not in KINDS or not isinstance(v, bool) for k, v in value.items()):
        return None, ServiceResponse.validation_error({'prefs': [f'Use true/false for: {", ".join(KINDS)}.']})
    return value, None


def register_device(*, user, session, token, platform, app_version='', locale='uz', prefs=None):
    token = str(token or '').strip()
    if not token or len(token) > 512:
        return ServiceResponse.validation_error({'token': ['A push token is required.']})
    if platform not in AdminDevice.Platform.values:
        return ServiceResponse.validation_error({'platform': ['Use ios or android.']})
    locale = locale if locale in LOCALES else 'uz'
    prefs, error = _clean_prefs(prefs)
    if error:
        return error
    with transaction.atomic():
        device = AdminDevice.objects.select_for_update().filter(token=token).first()
        if device is None:
            device = AdminDevice(token=token, prefs=prefs)
        elif prefs:
            device.prefs = {**(device.prefs or {}), **prefs}
        # A token moves with the phone: the latest login owns it.
        device.user = user
        device.session = session
        device.platform = platform
        device.app_version = str(app_version or '')[:32]
        device.locale = locale
        device.is_active = True
        device.save()
    return ServiceResponse.success(data={'device': serialize_device(device)}, message='Device registered')


def unregister_device(*, user, token):
    updated = AdminDevice.objects.filter(user=user, token=str(token or '').strip()).update(is_active=False)
    return ServiceResponse.success(data={'unregistered': updated})


def update_device(*, user, device_id, prefs=None, locale=None):
    device = AdminDevice.objects.filter(pk=device_id, user=user).first()
    if device is None:
        return ServiceResponse.not_found('Device not found')
    prefs, error = _clean_prefs(prefs)
    if error:
        return error
    fields = []
    if prefs:
        device.prefs = {**(device.prefs or {}), **prefs}
        fields.append('prefs')
    if locale in LOCALES:
        device.locale = locale
        fields.append('locale')
    if fields:
        device.save(update_fields=[*fields, 'last_seen_at'])
    return ServiceResponse.success(data={'device': serialize_device(device)})


def list_devices(*, user):
    rows = AdminDevice.objects.filter(user=user, is_active=True).order_by('-last_seen_at')
    return ServiceResponse.success(data={'devices': [serialize_device(d) for d in rows]})


# ---------------------------------------------------------------- events

def recipients(kind, exclude_user_id=None):
    now = timezone.now()
    devices = AdminDevice.objects.filter(
        is_active=True,
        user__role=User.RoleChoices.ADMIN,
        user__status=User.UserStatus.ACTIVE,
        user__is_deleted=False,
        session__isnull=False,
        session__expires_at__gt=now,
    ).select_related('user')
    if exclude_user_id:
        devices = devices.exclude(user_id=exclude_user_id)
    return [d for d in devices if d.wants(kind)]


def enqueue(kind, event_key, context, *, route='/', exclude_user_id=None):
    """Write one outbox row per interested device. Safe to call repeatedly."""
    if not enabled():
        return 0
    created = 0
    for device in recipients(kind, exclude_user_id):
        title_tpl, body_tpl = TEXT[kind].get(device.locale, TEXT[kind]['uz'])
        try:
            title, body = title_tpl.format(**context), body_tpl.format(**context)
        except (KeyError, IndexError):
            logger.exception('owner push template failed for %s', kind)
            continue
        try:
            with transaction.atomic():
                OwnerPushOutbox.objects.create(
                    device=device, event_key=event_key, kind=kind,
                    title=title[:120], body=' '.join(body.split())[:400],
                    data={'kind': kind, 'route': route, 'event': event_key},
                    next_attempt_at=timezone.now(),
                )
            created += 1
        except IntegrityError:
            pass  # already notified for this event
    return created


def _recent(day):
    return day is not None and day >= timezone.localdate() - timedelta(days=RECENT_DAYS)


def on_expense_pending(expense_id):
    from hr.models import Expense

    try:
        expense = Expense.objects.filter(pk=expense_id, is_deleted=False).first()
        if expense is None or expense.status != Expense.Status.PENDING or not _recent(expense.expense_date):
            return 0
        return enqueue(
            'expense_pending', f'expense:{expense.id}',
            {
                'amount': money(expense.amount),
                'category': expense.category_name_snapshot or '—',
                'description': f". {' '.join((expense.description or '').split())[:120]}" if (expense.description or '').strip() else '',
            },
            route=f'/approvals/{expense.id}',
            exclude_user_id=expense.created_by_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception('owner push: expense %s', expense_id)
        return 0


def on_shift_closed(shift_id):
    from base.models import Shift

    try:
        shift = Shift.objects.select_related('user').filter(pk=shift_id, is_deleted=False).first()
        if shift is None or shift.status != Shift.Status.ENDED:
            return 0
        ended = shift.end_time or timezone.now()
        if not _recent(timezone.localtime(ended).date()):
            return 0
        user = shift.user
        cashier = f'{user.first_name} {user.last_name}'.strip() if user else '—'
        return enqueue(
            'shift_closed', f'shift:{shift.id}',
            {'cashier': cashier, 'revenue': money(shift.total_revenue), 'orders': shift.total_orders or 0},
            route='/',
        )
    except Exception:  # noqa: BLE001
        logger.exception('owner push: shift %s', shift_id)
        return 0


def daily_summary_due(now=None):
    """The day being summarised once today's send time has passed, else None."""
    now = timezone.localtime(now or timezone.now(), TZ)
    hour, minute = (int(x) for x in getattr(settings, 'OWNER_DAILY_SUMMARY_AT', '09:00').split(':'))
    if now.time() < time(hour, minute):
        return None
    return now.date() - timedelta(days=1)


def send_daily_summary(day: date):
    from admins.services.owner_summary_service import get_owner_summary

    if not enabled():
        return 0
    summary = get_owner_summary(day, day)
    balances = summary['balances']
    return enqueue(
        'daily_summary', f'daily:{day.isoformat()}',
        {
            'day': day.strftime('%d.%m'),
            'sales': money(summary['sales']['net_sales_uzs']),
            'profit': money(summary['profit']['raw_profit_uzs']),
            'safe': money(balances['safe_uzs']),
            'bank': money(balances['bank_uzs']),
        },
        route='/',
    )


# ---------------------------------------------------------------- delivery

def backoff(attempts):
    return timedelta(seconds=min(30 * (2 ** max(attempts - 1, 0)), 3600))


def deliver_batch(limit=50):
    """Send due rows. Returns (sent, failed) counts."""
    now = timezone.now()
    sent = failed = 0
    with transaction.atomic():
        rows = list(
            OwnerPushOutbox.objects.select_for_update(skip_locked=True)
            .filter(status=OwnerPushOutbox.Status.PENDING, next_attempt_at__lte=now)
            .select_related('device')
            .order_by('next_attempt_at', 'id')[:limit]
        )
        for row in rows:
            device = row.device
            if not device.is_active:
                row.status = OwnerPushOutbox.Status.DEAD
                row.last_error = 'device inactive'
                row.save(update_fields=['status', 'last_error'])
                continue
            result = fcm.send(device.token, row.title, row.body, row.data)
            row.attempts += 1
            if result.ok:
                row.status = OwnerPushOutbox.Status.SENT
                row.sent_at = timezone.now()
                row.last_error = ''
                sent += 1
            else:
                failed += 1
                row.last_error = result.error[:300]
                if result.drop_device:
                    AdminDevice.objects.filter(pk=device.pk).update(is_active=False)
                    row.status = OwnerPushOutbox.Status.DEAD
                elif not result.retry or row.attempts >= MAX_ATTEMPTS:
                    row.status = OwnerPushOutbox.Status.DEAD
                else:
                    row.next_attempt_at = timezone.now() + backoff(row.attempts)
            row.save(update_fields=['status', 'attempts', 'sent_at', 'last_error', 'next_attempt_at'])
    return sent, failed


def purge(days=30):
    cutoff = timezone.now() - timedelta(days=days)
    return OwnerPushOutbox.objects.filter(created_at__lt=cutoff).exclude(status=OwnerPushOutbox.Status.PENDING).delete()[0]


def daily_summary_sent(day: date):
    return OwnerPushOutbox.objects.filter(event_key=f'daily:{day.isoformat()}').exists()
