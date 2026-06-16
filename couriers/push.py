"""Background push notifications via Expo Push (the courier app is Expo).

Best-effort: fired alongside the WebSocket events so a backgrounded app still
gets "new order" / "order ready". WebSocket covers the foreground. Never raises
into the caller — a push failure must not break the order flow.

Set EXPO_ACCESS_TOKEN in the env for higher rate limits (optional for dev).
Swap `_post` for an FCM call if you move off Expo.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger('couriers.push')

EXPO_ENDPOINT = 'https://exp.host/--/api/v2/push/send'


def push_to_courier(courier, title, body, data=None):
    """Send a push to every registered device of `courier`. Returns the count
    of messages accepted by Expo (0 on any failure / no tokens)."""
    tokens = list(courier.push_tokens.values_list('token', flat=True))
    if not tokens:
        return 0
    messages = [{
        'to': t,
        'title': title,
        'body': body,
        'sound': 'default',
        'priority': 'high',
        'data': data or {},
    } for t in tokens]
    return _post(messages)


def _post(messages):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    access = getattr(settings, 'EXPO_ACCESS_TOKEN', '') or ''
    if access:
        headers['Authorization'] = f'Bearer {access}'
    try:
        resp = requests.post(EXPO_ENDPOINT, json=messages, headers=headers, timeout=8)
        if resp.status_code != 200:
            logger.warning('expo push HTTP %s: %s', resp.status_code, resp.text[:200])
            return 0
        return len(messages)
    except Exception:  # noqa: BLE001 — push is best-effort
        logger.debug('expo push failed', exc_info=True)
        return 0
