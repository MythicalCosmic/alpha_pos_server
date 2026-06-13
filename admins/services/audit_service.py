from datetime import datetime

from django.core.paginator import Paginator
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.repositories import AuditLogRepository


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return timezone.make_aware(datetime.strptime(date_str, '%Y-%m-%d'))
    except (ValueError, TypeError):
        try:
            return timezone.make_aware(datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S'))
        except (ValueError, TypeError):
            return None


def _parse_date_to(date_str):
    """Parse an inclusive end-of-range bound.

    A bare date like '2026-05-31' parses to midnight, and filter_logs uses
    created_at__lte=date_to — so every log from that day was excluded. When
    only a date is supplied, roll it to the last microsecond of the day so the
    whole day is included. An explicit timestamp is honored as-is. (Mirrors
    order_service._parse_date_to.)
    """
    if not date_str:
        return None
    dt = _parse_date(date_str)
    if dt is None:
        return None
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0 \
            and len(date_str.strip()) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def _serialize(log):
    return {
        'id': log.id,
        'uuid': str(log.uuid),
        'action': log.action,
        'target_type': log.target_type or None,
        'target_id': log.target_id,
        'metadata': log.metadata or {},
        'ip_address': log.ip_address or None,
        'actor': {
            'id': log.actor.id,
            'name': f"{log.actor.first_name} {log.actor.last_name}".strip(),
            'email': log.actor.email,
            'role': log.actor.role,
        } if log.actor else None,
        'created_at': log.created_at.isoformat() if log.created_at else None,
    }


class AdminAuditService:

    @staticmethod
    def list_logs(page=1, per_page=20, action=None, actor_id=None,
                  target_type=None, target_id=None,
                  date_from=None, date_to=None):
        qs = AuditLogRepository.filter_logs(
            action=action,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            date_from=_parse_date(date_from),
            date_to=_parse_date_to(date_to),
        )
        paginator = Paginator(qs, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            'logs': [_serialize(log) for log in page_obj.object_list],
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_logs': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
            'filters': {
                'action': action,
                'actor_id': actor_id,
                'target_type': target_type,
                'target_id': target_id,
                'date_from': date_from,
                'date_to': date_to,
            },
        })
