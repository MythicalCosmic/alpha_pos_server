"""Demand forecasting via Claude.

Pulls the last 30 days of order history aggregated by product × weekday ×
hour, hands it to Claude, and asks for a prep-quantity recommendation for
tomorrow. Reuses the shared `base.services.llm` wiring (ANTHROPIC_API_KEY /
ANTHROPIC_MODEL) from the stock AI assistant.

The model call is isolated in `_call_llm` so tests can monkeypatch it
without configuring an API key.
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
DEFAULT_TOP_N = 15  # cap on products returned so a 200-item menu doesn't blow up the Gemini prompt


def gather_history(days=WINDOW_DAYS, top_n=DEFAULT_TOP_N):
    """Build the per-product × weekday × hour aggregate the forecaster reads.

    Returns a dict shaped like:
      {
        "window_days": 30,
        "products": [
          {
            "id": 12, "name": "Margherita",
            "total_qty": 145,
            "by_weekday": {"Mon": 20, "Tue": 18, ...},
            "by_hour": {"12": 25, "13": 30, ...},
          },
          ...
        ],
      }
    """
    from base.models import OrderItem, OrderRefund
    cutoff = timezone.now() - timedelta(days=days)

    # Top-N products by total quantity in the window — keeps the prompt
    # bounded and focuses Gemini on the products that actually drive prep.
    top = (
        OrderItem.objects.filter(
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__gte=cutoff,
        )
        # Cancelled orders never actually sold — counting them biases the prep
        # forecast upward.
        .values('product_id', 'product__name')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')
    )
    top_ids = [row['product_id'] for row in top]
    name_by_id = {row['product_id']: row['product__name'] for row in top}
    totals_by_id = {row['product_id']: int(row['total_qty'] or 0) for row in top}
    from base.services.refund_lines import (
        REFUND_EVENT_ALIAS, refund_item_events,
    )
    cancelled_items = refund_item_events(
        source=OrderRefund.Source.ORDER_CANCEL,
        refunded_at__gte=cutoff,
    )
    for row in cancelled_items.values(
        'product_id', 'product__name',
    ).annotate(total_qty=Sum('quantity')):
        pid = row['product_id']
        totals_by_id[pid] = totals_by_id.get(pid, 0) - int(row['total_qty'] or 0)
        name_by_id.setdefault(pid, row['product__name'])
    top_ids = [
        pid for pid, qty in sorted(
            totals_by_id.items(), key=lambda item: (-item[1], item[0]),
        ) if qty > 0
    ][:top_n]
    totals_by_id = {pid: totals_by_id[pid] for pid in top_ids}
    name_by_id = {pid: name_by_id[pid] for pid in top_ids}

    # One aggregate keyed on (product, weekday, hour) instead of N+1 fan-out.
    # Previously this fired top_n+1 queries; now it's 2.
    breakdown_rows = (
        OrderItem.objects.filter(
            product_id__in=top_ids,
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__gte=cutoff,
        )
        .values(
            'product_id',
            weekday=F('order__paid_at__week_day'),
            hour=F('order__paid_at__hour'),
        )
        .annotate(qty=Sum('quantity'))
    ) if top_ids else []

    weekday_map = {1: 'Sun', 2: 'Mon', 3: 'Tue', 4: 'Wed',
                   5: 'Thu', 6: 'Fri', 7: 'Sat'}
    per_product_weekday = {pid: {} for pid in top_ids}
    per_product_hour = {pid: {} for pid in top_ids}
    for cell in breakdown_rows:
        pid = cell['product_id']
        qty = int(cell['qty'] or 0)
        wd = weekday_map.get(cell['weekday'], str(cell['weekday']))
        per_product_weekday[pid][wd] = per_product_weekday[pid].get(wd, 0) + qty
        hr = str(cell['hour'])
        per_product_hour[pid][hr] = per_product_hour[pid].get(hr, 0) + qty
    refund_breakdown_rows = (
        cancelled_items.filter(product_id__in=top_ids)
        .values(
            'product_id',
            weekday=F(f'{REFUND_EVENT_ALIAS}__refunded_at__week_day'),
            hour=F(f'{REFUND_EVENT_ALIAS}__refunded_at__hour'),
        )
        .annotate(qty=Sum('quantity'))
    ) if top_ids else []
    for cell in refund_breakdown_rows:
        pid = cell['product_id']
        qty = int(cell['qty'] or 0)
        wd = weekday_map.get(cell['weekday'], str(cell['weekday']))
        per_product_weekday[pid][wd] = per_product_weekday[pid].get(wd, 0) - qty
        hr = str(cell['hour'])
        per_product_hour[pid][hr] = per_product_hour[pid].get(hr, 0) - qty

    products = [
        {
            'id': pid,
            'name': name_by_id[pid],
            'total_qty': totals_by_id[pid],
            'by_weekday': per_product_weekday[pid],
            'by_hour': per_product_hour[pid],
        }
        for pid in top_ids
    ]

    return {'window_days': days, 'products': products}


_PROMPT = """You are a restaurant prep planner. Given the last {days} days
of order history below, predict the quantity to prep tomorrow for each
product. Account for:

- Tomorrow is a {weekday_name}. Weight that weekday's history more heavily.
- Prefer slight over-prep to under-prep for high-margin items.
- Round to whole units.

Return JSON only, in this exact shape (no preamble, no markdown):

{{
  "tomorrow": "{tomorrow_iso}",
  "predictions": [
    {{"product_id": int, "product_name": str, "suggested_qty": int, "reason": "short string"}}
  ]
}}

DATA:
{data_json}
"""


def _call_llm(prompt_text):
    """Isolated so tests can monkeypatch without configuring an API key.

    Delegates to the shared AI wrapper (Claude or Gemini per AI_PROVIDER).
    Returns (text, error) where error is None on success, 'llm_sdk_missing' /
    'llm_key_missing' when unconfigured, or a raw error string otherwise."""
    from base.services.llm import call_ai
    return call_ai(prompt_text, max_tokens=2048)


def forecast_tomorrow():
    """Return (data, error). `data` is the parsed JSON from Gemini; `error`
    is a short code on failure."""
    history = gather_history()
    if not history['products']:
        return {'predictions': [], 'reason': 'no_history'}, None

    tomorrow = timezone.now() + timedelta(days=1)
    weekday_name = tomorrow.strftime('%A')

    prompt = _PROMPT.format(
        days=WINDOW_DAYS,
        weekday_name=weekday_name,
        tomorrow_iso=tomorrow.date().isoformat(),
        data_json=json.dumps(history, ensure_ascii=False),
    )

    raw, err = _call_llm(prompt)
    if err:
        return None, err

    # The model sometimes wraps JSON in ``` fences despite instructions.
    text = raw.strip()
    if text.startswith('```'):
        text = text.strip('`')
        # Drop a leading "json" language tag if present.
        if text.lower().startswith('json'):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        logger.warning('llm returned non-JSON forecast: %s', raw[:200])
        return None, 'parse_error'
    return parsed, None
