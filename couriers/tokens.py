"""One-time courier QR claims and rotating mobile-session credentials.

The raw values returned to clients are high-entropy bearer credentials.  The
database stores only SHA-256 digests, matching the core SessionRepository's
handling of access tokens.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import secrets
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from base.models import Session
from base.repositories.session import SessionRepository

from couriers.models import Courier, CourierLoginClaim, CourierRefreshToken


QR_CLAIM_PREFIX = 'cq1.'
REFRESH_TOKEN_PREFIX = 'cr1.'
ACCESS_TOKEN_TTL = timedelta(days=7)
REFRESH_TOKEN_TTL = timedelta(days=30)
QR_CLAIM_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime

    def response_payload(self):
        return {
            # ``token`` is retained for compatibility with the courier app.
            'token': self.access_token,
            'token_type': 'Token',
            'expires_at': self.access_expires_at.isoformat(),
            'refresh_token': self.refresh_token,
            'refresh_expires_at': self.refresh_expires_at.isoformat(),
        }


def _digest(raw_token):
    if not raw_token:
        return None
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def _random_token(prefix):
    # token_urlsafe(32) carries 256 bits from secrets' CSPRNG.
    return prefix + secrets.token_urlsafe(32)


def _ttl(setting_name, default):
    seconds = getattr(settings, setting_name, None)
    if seconds is None:
        return default
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return default
    return timedelta(seconds=max(1, seconds))


def issue_login_claim(courier, *, issued_by=None):
    """Rotate a courier's outstanding QR claims and return the new raw claim."""
    now = timezone.now()
    expires_at = now + _ttl('COURIER_QR_CLAIM_TTL_SECONDS', QR_CLAIM_TTL)
    raw_claim = _random_token(QR_CLAIM_PREFIX)
    with transaction.atomic():
        # Lock the courier row so simultaneous regenerate requests serialize.
        locked = Courier.objects.select_for_update().get(pk=courier.pk)
        CourierLoginClaim.objects.filter(
            courier=locked,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
        ).update(revoked_at=now)
        CourierLoginClaim.objects.create(
            courier=locked,
            issued_by=issued_by,
            token_digest=_digest(raw_claim),
            expires_at=expires_at,
        )
    return raw_claim, expires_at


def consume_login_claim(raw_claim):
    """Atomically consume a QR claim, returning its courier or ``None``."""
    if not raw_claim or not raw_claim.startswith(QR_CLAIM_PREFIX):
        return None
    now = timezone.now()
    with transaction.atomic():
        claim = (CourierLoginClaim.objects.select_for_update()
                 .select_related('courier__user')
                 .filter(token_digest=_digest(raw_claim))
                 .first())
        if (claim is None or claim.consumed_at is not None
                or claim.revoked_at is not None or claim.expires_at <= now):
            return None
        claim.consumed_at = now
        claim.save(update_fields=['consumed_at'])
        return claim.courier


def _delete_access_session(session):
    if session is None:
        return
    # SessionRepository caches by the already-hashed payload.  Deleting this
    # exact key closes the otherwise five-minute cache window immediately.
    if session.payload:
        cache.delete(f'session:{session.payload}')
    session.delete()


def _issue_session(courier, *, ip_address, user_agent, family_id=None,
                   refresh_expires_at=None):
    now = timezone.now()
    access_expires_at = now + _ttl(
        'COURIER_ACCESS_TOKEN_TTL_SECONDS', ACCESS_TOKEN_TTL,
    )
    if refresh_expires_at is None:
        refresh_expires_at = now + _ttl(
            'COURIER_REFRESH_TOKEN_TTL_SECONDS', REFRESH_TOKEN_TTL,
        )
    # A refresh family has an absolute lifetime; a last-minute refresh cannot
    # create an access token that outlives that family.
    access_expires_at = min(access_expires_at, refresh_expires_at)

    access_token = secrets.token_hex(32)
    refresh_token = _random_token(REFRESH_TOKEN_PREFIX)
    session = SessionRepository.create(
        user_id=courier.user,
        ip_address=ip_address,
        user_agent=user_agent,
        payload=SessionRepository.hash_token(access_token),
        expires_at=access_expires_at,
    )
    row = CourierRefreshToken.objects.create(
        courier=courier,
        access_session=session,
        token_digest=_digest(refresh_token),
        family_id=family_id or uuid.uuid4(),
        expires_at=refresh_expires_at,
    )
    return IssuedSession(
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
    ), row


def issue_session(courier, *, ip_address, user_agent):
    with transaction.atomic():
        issued, _row = _issue_session(
            courier, ip_address=ip_address, user_agent=user_agent,
        )
    return issued


def _revoke_family(family_id, *, at=None):
    """Revoke a locked refresh family and immediately delete access sessions."""
    at = at or timezone.now()
    rows = list(
        CourierRefreshToken.objects.select_for_update()
        .filter(family_id=family_id)
        .select_related('access_session')
    )
    for row in rows:
        if row.revoked_at is None:
            row.revoked_at = at
            row.save(update_fields=['revoked_at'])
        if row.access_session_id:
            _delete_access_session(row.access_session)


def rotate_refresh_token(raw_refresh, *, ip_address, user_agent):
    """Rotate once; replay of an already-used token revokes the whole family."""
    if not raw_refresh or not raw_refresh.startswith(REFRESH_TOKEN_PREFIX):
        return None
    now = timezone.now()
    with transaction.atomic():
        row = (CourierRefreshToken.objects.select_for_update()
               .select_related('courier__user', 'access_session')
               .filter(token_digest=_digest(raw_refresh))
               .first())
        if row is None:
            return None
        if row.used_at is not None:
            # Replay is evidence that the old credential may have been stolen.
            _revoke_family(row.family_id, at=now)
            return None
        if row.revoked_at is not None or row.expires_at <= now:
            return None
        courier = row.courier
        user = courier.user
        if user.is_deleted or getattr(user, 'status', 'ACTIVE') != 'ACTIVE':
            _revoke_family(row.family_id, at=now)
            return None

        row.used_at = now
        row.save(update_fields=['used_at'])
        if row.access_session_id:
            _delete_access_session(row.access_session)
        issued, replacement = _issue_session(
            courier,
            ip_address=ip_address,
            user_agent=user_agent,
            family_id=row.family_id,
            refresh_expires_at=row.expires_at,
        )
        # Deleting the old Session makes Django's cached related object look
        # unsaved on ``row``.  A scoped update records the audit link without
        # touching that stale in-memory relation.
        CourierRefreshToken.objects.filter(pk=row.pk).update(
            replaced_by=replacement,
        )
        return issued


def revoke_refresh_token(raw_refresh):
    """Idempotently revoke the family identified by a refresh token."""
    if not raw_refresh or not raw_refresh.startswith(REFRESH_TOKEN_PREFIX):
        return
    with transaction.atomic():
        row = (CourierRefreshToken.objects.select_for_update()
               .filter(token_digest=_digest(raw_refresh)).first())
        if row is not None:
            _revoke_family(row.family_id)


def revoke_access_token(raw_access):
    """Revoke an access token and its refresh family, if one exists."""
    token_hash = SessionRepository.hash_token(raw_access)
    if not token_hash:
        return
    with transaction.atomic():
        session = Session.objects.select_for_update().filter(payload=token_hash).first()
        if session is None:
            SessionRepository.invalidate_cache(raw_access)
            return
        row = (CourierRefreshToken.objects.select_for_update()
               .filter(access_session=session).first())
        if row is not None:
            _revoke_family(row.family_id)
        else:
            _delete_access_session(session)


def revoke_all_for_courier(courier):
    """Revoke all mobile sessions, used when a manager resets a password."""
    with transaction.atomic():
        family_ids = list(
            CourierRefreshToken.objects.filter(courier=courier)
            .values_list('family_id', flat=True).distinct()
        )
        for family_id in family_ids:
            _revoke_family(family_id)
        # Also cover legacy sessions issued before refresh records existed.
        legacy = Session.objects.filter(user_id=courier.user)
        for session in list(legacy):
            _delete_access_session(session)
