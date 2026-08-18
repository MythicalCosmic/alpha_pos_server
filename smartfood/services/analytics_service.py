"""Telegram Mini App visit and conversion analytics for the bot admin."""
from datetime import datetime, time, timedelta
from uuid import UUID

from django.db.models import Count, Max, Min, Q, Subquery
from django.db.models.functions import TruncDate
from django.utils import timezone

from base.helpers.response import ServiceResponse
from smartfood.models import BotOrder, BotProduct, BotVisit, Customer


_ALLOWED_RANGES = (7, 30, 90)


def _range_days(raw):
    try:
        value = int(raw or 30)
    except (TypeError, ValueError):
        value = 30
    return value if value in _ALLOWED_RANGES else 30


def _bounds(days):
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    first_day = today - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(first_day, time.min), tz)
    end = timezone.make_aware(
        datetime.combine(today + timedelta(days=1), time.min),
        tz,
    )
    previous_start = start - timedelta(days=days)
    return first_day, start, end, previous_start, tz


def _change(current, previous):
    if previous == 0:
        return None if current else 0.0
    return round(((current - previous) / previous) * 100, 1)


def _period_metrics(start, end):
    visits = BotVisit.objects.filter(visited_at__gte=start, visited_at__lt=end)
    visitor_ids = visits.values('customer_id').distinct()
    orders = BotOrder.objects.filter(created_at__gte=start, created_at__lt=end)

    unique_visitors = visitor_ids.count()
    converted_visitors = (
        orders.filter(customer_id__in=Subquery(visitor_ids))
        .values('customer_id')
        .distinct()
        .count()
    )
    order_count = orders.count()
    return {
        'app_opens': visits.count(),
        'unique_visitors': unique_visitors,
        'converted_visitors': converted_visitors,
        'orders': order_count,
        'conversion_rate': round(
            (converted_visitors / unique_visitors) * 100,
            1,
        ) if unique_visitors else 0.0,
    }


class BotVisitService:
    @staticmethod
    def record(customer, client_visit_id, user_agent='', ip_address=''):
        try:
            visit_id = UUID(str(client_visit_id))
        except (TypeError, ValueError, AttributeError):
            return ServiceResponse.validation_error({
                'client_visit_id': 'A valid UUID is required.',
            })

        visit, created = BotVisit.objects.get_or_create(
            client_visit_id=visit_id,
            defaults={
                'customer': customer,
                'user_agent': (user_agent or '')[:256],
                'ip_address': (ip_address or '')[:45],
            },
        )
        # A UUID cannot be replayed to attribute somebody else's visit.
        if visit.customer_id != customer.id:
            return ServiceResponse.validation_error({
                'client_visit_id': 'Visit identifier already belongs to another user.',
            })
        return ServiceResponse.success(
            data={
                'id': visit.id,
                'recorded': created,
                'visited_at': visit.visited_at.isoformat(),
            },
            message='Visit recorded' if created else 'Visit already recorded',
        )


class BotAnalyticsService:
    @staticmethod
    def overview(days=30):
        days = _range_days(days)
        first_day, start, end, previous_start, tz = _bounds(days)
        current = _period_metrics(start, end)
        previous = _period_metrics(previous_start, start)

        visits_by_day = {
            row['day']: row
            for row in (
                BotVisit.objects.filter(visited_at__gte=start, visited_at__lt=end)
                .annotate(day=TruncDate('visited_at', tzinfo=tz))
                .values('day')
                .annotate(
                    app_opens=Count('id'),
                    unique_visitors=Count('customer_id', distinct=True),
                )
            )
        }
        visitor_ids_by_day = {}
        for row in (
            BotVisit.objects.filter(visited_at__gte=start, visited_at__lt=end)
            .annotate(day=TruncDate('visited_at', tzinfo=tz))
            .values('day', 'customer_id')
            .distinct()
        ):
            visitor_ids_by_day.setdefault(row['day'], set()).add(
                row['customer_id']
            )

        orders_by_day = {
            row['day']: row
            for row in (
                BotOrder.objects.filter(created_at__gte=start, created_at__lt=end)
                .annotate(day=TruncDate('created_at', tzinfo=tz))
                .values('day')
                .annotate(orders=Count('id'))
            )
        }
        converted_ids_by_day = {}
        for row in (
            BotOrder.objects.filter(created_at__gte=start, created_at__lt=end)
            .annotate(day=TruncDate('created_at', tzinfo=tz))
            .values('day', 'customer_id')
            .distinct()
        ):
            if row['customer_id'] in visitor_ids_by_day.get(row['day'], set()):
                converted_ids_by_day.setdefault(row['day'], set()).add(
                    row['customer_id']
                )

        series = []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            visit_row = visits_by_day.get(day, {})
            order_row = orders_by_day.get(day, {})
            series.append({
                'date': day.isoformat(),
                'app_opens': visit_row.get('app_opens', 0),
                'unique_visitors': visit_row.get('unique_visitors', 0),
                'orders': order_row.get('orders', 0),
                'converted_visitors': len(converted_ids_by_day.get(day, set())),
            })

        metric_changes = {
            key: _change(current[key], previous[key])
            for key in ('app_opens', 'unique_visitors', 'converted_visitors', 'orders')
        }
        metric_changes['conversion_rate'] = round(
            current['conversion_rate'] - previous['conversion_rate'],
            1,
        )

        catalog_qs = BotProduct.objects.filter(
            is_published=True,
            product__is_deleted=False,
        )
        catalog = {
            'published_products': catalog_qs.count(),
            'available_products': catalog_qs.filter(is_selling=True).count(),
            'with_image': catalog_qs.exclude(image_url='').count(),
            'missing_image': catalog_qs.filter(image_url='').count(),
        }

        return ServiceResponse.success(data={
            'period': {
                'days': days,
                'from': first_day.isoformat(),
                'to': timezone.localdate().isoformat(),
            },
            'metrics': current,
            'previous_metrics': previous,
            'changes': metric_changes,
            'series': series,
            'catalog': catalog,
            'definitions': {
                'app_opens': 'Recorded Telegram Mini App page boots.',
                'unique_visitors': 'Distinct Telegram users who opened the Mini App.',
                'converted_visitors': 'Visitors who placed at least one Telegram order in the same period.',
                'conversion_rate': 'Converted visitors divided by unique visitors.',
            },
        })

    @staticmethod
    def visitors(q='', converted='', page=1, per_page=20):
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = min(100, max(1, int(per_page or 20)))
        except (TypeError, ValueError):
            per_page = 20

        qs = (
            Customer.objects.annotate(
                first_visit_at=Min('visits__visited_at'),
                last_visit_at=Max('visits__visited_at'),
                visit_count=Count('visits', distinct=True),
                order_count=Count('orders', distinct=True),
                last_order_at=Max('orders__created_at'),
            )
            .filter(visit_count__gt=0)
        )
        query = (q or '').strip()
        if query:
            qs = qs.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(username__icontains=query)
                | Q(phone_number__icontains=query)
                | Q(telegram_id__icontains=query)
            )
        if str(converted).lower() in ('true', '1', 'yes'):
            qs = qs.filter(order_count__gt=0)
        elif str(converted).lower() in ('false', '0', 'no'):
            qs = qs.filter(order_count=0)

        qs = qs.order_by('-last_visit_at', '-id')
        total = qs.count()
        offset = (page - 1) * per_page
        customers = qs[offset:offset + per_page]
        items = [{
            'id': customer.id,
            'telegram_id': customer.telegram_id,
            'username': customer.username,
            'name': customer.name,
            'phone': customer.phone_number,
            'language': customer.language,
            'photo_url': customer.photo_url,
            'first_visit_at': (
                customer.first_visit_at.isoformat()
                if customer.first_visit_at else None
            ),
            'last_visit_at': (
                customer.last_visit_at.isoformat()
                if customer.last_visit_at else None
            ),
            'visit_count': customer.visit_count,
            'order_count': customer.order_count,
            'last_order_at': (
                customer.last_order_at.isoformat()
                if customer.last_order_at else None
            ),
            'converted': customer.order_count > 0,
        } for customer in customers]

        return ServiceResponse.success(data={
            'items': items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': (total + per_page - 1) // per_page,
            },
        })
