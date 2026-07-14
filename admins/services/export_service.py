"""1C export.

Generates CommerceML 2.05-flavored XML for completed orders in a date
window. The Cyrillic element names follow the standard 1C-Bitrix and
typical 1C accounting templates so a venue's accountant can ingest with
little or no mapping work.

V1 emits Документ-per-Order rows with embedded Товары. Subscription /
companies / contractors are not included — those typically live in 1C
already and are matched by name + phone at ingest.

This is *not* a full CommerceML implementation — it covers the order
shape we have today (line items + totals + payment method). If a
specific 1C config needs additional fields, extend `_serialize_order`.
"""
import logging
from datetime import datetime, timezone as _tz
from xml.etree import ElementTree as ET

from django.utils import timezone
from django.utils.dateparse import parse_date

from base.models import Order

logger = logging.getLogger(__name__)


def _payment_method_label(method):
    # Accounting document: keep the STORED tender (acquirer detail is wanted here,
    # unlike the reporting layer which folds Uzcard/Humo/Card into one `card`).
    return {
        'CASH': 'Наличные',
        'UZCARD': 'Uzcard',
        'HUMO': 'Humo',
        'CARD': 'Карта',
        'PAYME': 'Payme',
        'MIXED': 'Смешанная',
    }.get(method, method or '')


def _decimal(value):
    # CommerceML wants plain decimal strings, not "100000.00" — but most
    # ingest tools accept the latter.
    return f'{value:.2f}'


def _date(dt):
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.date().isoformat()


def _serialize_order(parent, order, document_at=None):
    doc = ET.SubElement(parent, 'Документ')
    ET.SubElement(doc, 'Ид').text = str(order.id)
    ET.SubElement(doc, 'Номер').text = str(order.display_id)
    ET.SubElement(doc, 'Дата').text = _date(document_at or order.created_at)
    ET.SubElement(doc, 'ХозОперация').text = 'Заказ товара'
    ET.SubElement(doc, 'Роль').text = 'Продавец'
    ET.SubElement(doc, 'Валюта').text = 'UZS'
    ET.SubElement(doc, 'Сумма').text = _decimal(order.total_amount)
    ET.SubElement(doc, 'Оплачен').text = 'true' if order.is_paid else 'false'
    if order.payment_method:
        ET.SubElement(doc, 'ФормаОплаты').text = _payment_method_label(order.payment_method)
    if order.phone_number:
        ET.SubElement(doc, 'ТелефонКонтакта').text = order.phone_number

    # Linked client (base.Customer) as the CommerceML buyer counterparty, so
    # 1C/ingest sees who the order belongs to. Walk-in orders have no customer.
    if order.customer_id:
        cps = ET.SubElement(doc, 'Контрагенты')
        cp = ET.SubElement(cps, 'Контрагент')
        ET.SubElement(cp, 'Ид').text = str(order.customer.uuid)
        ET.SubElement(cp, 'Наименование').text = order.customer.name or ''
        ET.SubElement(cp, 'Роль').text = 'Покупатель'
        if order.customer.phone_number:
            ET.SubElement(cp, 'Телефон').text = order.customer.phone_number

    items_el = ET.SubElement(doc, 'Товары')
    # The outer build_export call prefetches `items__product`, so iterate the
    # cached attribute instead of issuing a fresh `.select_related('product')`
    # query per order — that fan-out turns a month-window export of 1000
    # orders into 1001 queries instead of 2.
    for item in order.items.all():
        item_el = ET.SubElement(items_el, 'Товар')
        ET.SubElement(item_el, 'Ид').text = str(item.product_id)
        ET.SubElement(item_el, 'Наименование').text = item.product.name
        ET.SubElement(item_el, 'Количество').text = str(item.quantity)
        ET.SubElement(item_el, 'ЦенаЗаЕдиницу').text = _decimal(item.price)
        ET.SubElement(item_el, 'Сумма').text = _decimal(item.price * item.quantity)


def build_export(date_from, date_to, include_unpaid=False):
    """Return (xml_bytes, count) for completed orders in the window.

    `date_from`/`date_to` are date objects (inclusive on both ends).
    Cancelled orders are always excluded. Unpaid completed orders are
    only included when include_unpaid is True — most 1C ingest flows
    want only realized revenue.
    """
    from django.db.models import Prefetch
    from base.models import OrderItem
    qs = (
        Order.objects.filter(status='COMPLETED', is_deleted=False)
        .select_related('user', 'cashier', 'customer')
        .prefetch_related(
            Prefetch(
                'items',
                queryset=OrderItem.objects.filter(is_deleted=False).select_related('product'),
            ),
        )
    )
    if include_unpaid:
        # A mixed operational export can include tickets with no settlement
        # event, so its window and document date remain creation-based.
        qs = qs.filter(
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        ).order_by('created_at')
    else:
        # The default export is realized accounting data. Both selection and
        # document ordering follow settlement, preventing a cross-midnight sale
        # from landing in the wrong 1C period.
        qs = qs.filter(
            is_paid=True,
            paid_at__date__gte=date_from,
            paid_at__date__lte=date_to,
        ).order_by('paid_at')

    root = ET.Element('КоммерческаяИнформация', {
        'ВерсияСхемы': '2.05',
        'ДатаФормирования': datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%S'),
    })

    count = 0
    # iterator() streams orders without materialising the whole result set —
    # a year-wide export on a busy venue would otherwise hold every Order +
    # every OrderItem + every Product in memory.
    for order in qs.iterator(chunk_size=200):
        document_at = order.created_at if include_unpaid else order.paid_at
        _serialize_order(root, order, document_at=document_at)
        count += 1

    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    return xml, count


def parse_date_range(date_from_str, date_to_str):
    """Parse YYYY-MM-DD inputs into date objects. Returns (from, to, err)."""
    if not date_from_str or not date_to_str:
        return None, None, 'date_from and date_to are required (YYYY-MM-DD)'
    df = parse_date(date_from_str)
    dt = parse_date(date_to_str)
    if not df or not dt:
        return None, None, 'Invalid date format (expected YYYY-MM-DD)'
    if df > dt:
        return None, None, 'date_from must be on or before date_to'
    return df, dt, None
