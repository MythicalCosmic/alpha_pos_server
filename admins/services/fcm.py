"""Firebase Cloud Messaging (HTTP v1) sender for the owner app.

Android is delivered directly by FCM; iOS goes FCM -> APNs using the APNs key
uploaded in the Firebase console, so this server holds only the Firebase
service-account file (``FCM_SERVICE_ACCOUNT_FILE``) and ``FCM_PROJECT_ID``.
"""
import logging
import threading
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger('admins.fcm')

SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
ENDPOINT = 'https://fcm.googleapis.com/v1/projects/{project}/messages:send'
TIMEOUT_SECONDS = 10

_credentials = None
_credentials_lock = threading.Lock()


@dataclass(frozen=True)
class SendResult:
    ok: bool
    retry: bool = False
    drop_device: bool = False
    error: str = ''


def is_configured():
    return bool(getattr(settings, 'FCM_PROJECT_ID', '') and getattr(settings, 'FCM_SERVICE_ACCOUNT_FILE', ''))


def _access_token(force_refresh=False):
    global _credentials
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    with _credentials_lock:
        if _credentials is None:
            _credentials = service_account.Credentials.from_service_account_file(
                settings.FCM_SERVICE_ACCOUNT_FILE, scopes=[SCOPE],
            )
        if force_refresh or not _credentials.valid:
            _credentials.refresh(Request())
        return _credentials.token


def build_message(token, title, body, data=None):
    """FCM v1 message; ``data`` values must be strings for FCM."""
    return {
        'message': {
            'token': token,
            'notification': {'title': title, 'body': body},
            'data': {str(k): str(v) for k, v in (data or {}).items()},
            'android': {'priority': 'high', 'notification': {'channel_id': 'owner', 'sound': 'default'}},
            'apns': {'payload': {'aps': {'sound': 'default'}}},
        },
    }


def send(token, title, body, data=None):
    """Send one message. Never raises: failures come back as a SendResult."""
    if not is_configured():
        return SendResult(ok=False, retry=True, error='FCM is not configured')
    url = ENDPOINT.format(project=settings.FCM_PROJECT_ID)
    payload = build_message(token, title, body, data)
    for attempt in range(2):
        try:
            response = requests.post(
                url, json=payload, timeout=TIMEOUT_SECONDS,
                headers={'Authorization': f'Bearer {_access_token(force_refresh=attempt > 0)}'},
            )
        except Exception as exc:  # noqa: BLE001 — network/credential errors are retried by the worker
            logger.warning('fcm send failed: %s', exc)
            return SendResult(ok=False, retry=True, error=str(exc)[:300])
        if response.status_code == 200:
            return SendResult(ok=True)
        if response.status_code == 401 and attempt == 0:
            continue
        return _classify(response)
    return SendResult(ok=False, retry=True, error='FCM authentication failed')


def _classify(response):
    try:
        error = response.json().get('error', {})
    except ValueError:
        error = {}
    status = error.get('status', '')
    details = ' '.join(d.get('errorCode', '') for d in error.get('details', []) if isinstance(d, dict))
    text = f"{response.status_code} {status} {details} {error.get('message', '')}".strip()[:300]
    if response.status_code == 404 or 'UNREGISTERED' in details or status == 'NOT_FOUND':
        return SendResult(ok=False, drop_device=True, error=text)
    if response.status_code == 400 and ('INVALID_ARGUMENT' in (status + details)):
        return SendResult(ok=False, drop_device=True, error=text)
    if response.status_code in (429, 500, 502, 503, 504):
        return SendResult(ok=False, retry=True, error=text)
    return SendResult(ok=False, retry=False, error=text)
