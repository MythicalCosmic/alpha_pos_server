"""Manager-facing Telegram customer registry and account detail metrics."""

from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    Count,
    DecimalField,
    Max,
    Min,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, Trim
from django.utils import timezone

from base.helpers.response import ServiceResponse
from smartfood.models import BotOrder, Customer
from smartfood.serializers import address_dict, bot_order_dict, uzs


def _pagination(page, per_page):
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(100, max(1, int(per_page or 20)))
    except (TypeError, ValueError):
        per_page = 20
    return page, per_page


def _truthy(raw):
    value = str(raw or '').lower()
    if value in ('true', '1', 'yes'):
        return True
    if value in ('false', '0', 'no'):
        return False
    return None


def _profile_complete_customers(queryset):
    """Apply the same identity predicate as Customer.profile_complete."""
    return (
        queryset
        .alias(_clean_first_name=Trim('first_name'))
        .alias(_clean_last_name=Trim('last_name'))
        .filter(
            profile_confirmed_at__isnull=False,
            phone_number__regex=r'^998[0-9]{9}$',
        )
        .exclude(_clean_first_name='')
        .exclude(_clean_last_name='')
    )


def _customer_rows():
    accepted = (
        Q(orders__status=BotOrder.Status.DISPATCHED)
        & ~Q(orders__pos_order__status='CANCELED')
    )
    spend = (
        BotOrder.objects
        .filter(
            customer_id=OuterRef('pk'),
            status=BotOrder.Status.DISPATCHED,
            pos_order__is_paid=True,
        )
        .exclude(pos_order__status='CANCELED')
        .values('customer_id')
        .annotate(total=Sum('total'))
        .values('total')[:1]
    )
    return Customer.objects.annotate(
        first_visit_at=Min('visits__visited_at'),
        last_visit_at=Max('visits__visited_at'),
        visit_count=Count('visits', distinct=True),
        address_count=Count('addresses', distinct=True),
        deliverable_address_count=Count(
            'addresses',
            filter=(Q(addresses__lat__isnull=False) & Q(addresses__lng__isnull=False)),
            distinct=True,
        ),
        order_count=Count('orders', distinct=True),
        accepted_order_count=Count('orders', filter=accepted, distinct=True),
        last_order_at=Max('orders__created_at'),
        total_spent=Coalesce(
            Subquery(
                spend,
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            Value(Decimal('0.00')),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
    )


def _row(customer):
    return {
        'id': customer.id,
        'telegram_id': customer.telegram_id,
        'username': customer.username,
        'first_name': customer.first_name,
        'last_name': customer.last_name,
        'name': customer.name,
        'phone': customer.phone_number,
        'language': customer.language,
        'photo_url': customer.photo_url,
        'profile_complete': customer.profile_complete,
        'profile_missing': customer.profile_missing,
        'profile_confirmed_at': (
            customer.profile_confirmed_at.isoformat()
            if customer.profile_confirmed_at else None
        ),
        'loyalty_points': customer.loyalty_points,
        'is_blocked': customer.is_blocked,
        'broadcast_opted_in': customer.broadcast_opted_in,
        'telegram_reachable': customer.telegram_reachable,
        'created_at': customer.created_at.isoformat(),
        'first_visit_at': (
            customer.first_visit_at.isoformat()
            if customer.first_visit_at else None
        ),
        'last_visit_at': (
            customer.last_visit_at.isoformat()
            if customer.last_visit_at else None
        ),
        'visit_count': customer.visit_count,
        'address_count': customer.address_count,
        'deliverable_address_count': customer.deliverable_address_count,
        'order_count': customer.order_count,
        'accepted_order_count': customer.accepted_order_count,
        'last_order_at': (
            customer.last_order_at.isoformat()
            if customer.last_order_at else None
        ),
        'total_spent': uzs(customer.total_spent),
        'broadcast_eligible': (
            not customer.is_blocked
            and customer.broadcast_opted_in
            and customer.telegram_reachable
        ),
    }


class AdminCustomerService:
    @staticmethod
    def summary():
        now = timezone.now()
        thirty_days_ago = now - timedelta(days=30)
        qs = Customer.objects.all()
        accepted_customer_ids = (
            BotOrder.objects
            .filter(status=BotOrder.Status.DISPATCHED)
            .exclude(pos_order__status='CANCELED')
            .values('customer_id')
        )
        language_rows = qs.values('language').annotate(count=Count('id'))
        data = {
            'total_users': qs.count(),
            'new_30d': qs.filter(created_at__gte=thirty_days_ago).count(),
            'active_30d': qs.filter(
                visits__visited_at__gte=thirty_days_ago,
            ).distinct().count(),
            'profile_complete': _profile_complete_customers(qs).count(),
            'with_phone': qs.exclude(phone_number='').count(),
            'customers_with_orders': qs.filter(
                id__in=accepted_customer_ids,
            ).count(),
            'eligible_broadcast': qs.filter(
                is_blocked=False,
                broadcast_opted_in=True,
                telegram_reachable=True,
            ).count(),
            'languages': {'uz': 0, 'ru': 0, 'en': 0},
        }
        for row in language_rows:
            if row['language'] in data['languages']:
                data['languages'][row['language']] = row['count']
        return ServiceResponse.success(data=data)

    @staticmethod
    def list(*, q='', language='', profile='', ordered='', page=1, per_page=20):
        page, per_page = _pagination(page, per_page)
        qs = _customer_rows()
        query = str(q or '').strip()
        if query:
            qs = qs.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(username__icontains=query)
                | Q(phone_number__icontains=query)
                | Q(telegram_id__icontains=query)
            )
        if language in ('uz', 'ru', 'en'):
            qs = qs.filter(language=language)
        complete_ids = _profile_complete_customers(
            Customer.objects.all(),
        ).values('id')
        if profile == 'complete':
            qs = qs.filter(id__in=complete_ids)
        elif profile == 'incomplete':
            qs = qs.exclude(id__in=complete_ids)
        ordered_value = _truthy(ordered)
        if ordered_value is True:
            qs = qs.filter(order_count__gt=0)
        elif ordered_value is False:
            qs = qs.filter(order_count=0)

        qs = qs.order_by('-last_visit_at', '-created_at', '-id')
        total = qs.count()
        offset = (page - 1) * per_page
        return ServiceResponse.success(data={
            'items': [_row(customer) for customer in qs[offset:offset + per_page]],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': (total + per_page - 1) // per_page,
            },
        })

    @staticmethod
    def detail(customer_id):
        customer = _customer_rows().filter(id=customer_id).first()
        if customer is None:
            return ServiceResponse.not_found('Customer not found')
        recent_orders = (
            customer.orders
            .select_related('pos_order')
            .prefetch_related('items')
            .order_by('-id')[:20]
        )
        data = _row(customer)
        data['addresses'] = [
            address_dict(address)
            for address in customer.addresses.order_by('-is_default', '-id')
        ]
        data['recent_orders'] = [bot_order_dict(order) for order in recent_orders]
        return ServiceResponse.success(data=data)
