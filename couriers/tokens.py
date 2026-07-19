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

from base.models import Session, User
from base.repositories.session import SessionRepository

from couriers.models import Courier, CourierLoginClaim, CourierRefreshToken


QR_CLAIM_PREFIX = 'cq1.'
REFRESH_TOKEN_PREFIX = 'cr1.'
ACCESS_TOKEN_TTL = timedelta(days=7)
REFRESH_TOKEN_TTL = timedelta(days=30)
QR_CLAIM_TTL = timedelta(minutes=10)


class CourierTokenAudienceError(ValueError):
    """Raised when a non-courier identity attempts to mint courier tokens."""


def _require_courier_identity(courier):
    user = courier.user
    if (
        user.role != User.RoleChoices.COURIER
        or user.is_deleted
        or getattr(user, 'status', User.UserStatus.ACTIVE)
        != User.UserStatus.ACTIVE
    ):
        raise CourierTokenAudienceError(
            'Courier tokens require an active COURIER identity',
        )
    return user


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


def _lock_courier(courier_or_id):
    """Acquire the root lock for every courier credential mutation.

    Keeping the order Courier -> refresh/claim rows -> Session rows avoids the
    inverse lock ordering that used to make refresh, logout, and password reset
    capable of deadlocking one another under load.
    """
    courier_id = getattr(courier_or_id, 'pk', courier_or_id)
    return Courier.objects.select_for_update().get(pk=courier_id)


def issue_login_claim(courier, *, issued_by=None):
    """Rotate a courier's outstanding QR claims and return the new raw claim."""
    now = timezone.now()
    expires_at = now + _ttl('COURIER_QR_CLAIM_TTL_SECONDS', QR_CLAIM_TTL)
    raw_claim = _random_token(QR_CLAIM_PREFIX)
    with transaction.atomic():
        # Lock the courier row so simultaneous regenerate requests serialize.
        locked = _lock_courier(courier)
        _require_courier_identity(locked)
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
    candidate = (CourierLoginClaim.objects
                 .filter(token_digest=_digest(raw_claim))
                 .values('pk', 'courier_id')
                 .first())
    if candidate is None:
        return None
    with transaction.atomic():
        courier = _lock_courier(candidate['courier_id'])
        try:
            _require_courier_identity(courier)
        except CourierTokenAudienceError:
            return None
        # Conditional UPDATE is the actual one-time gate.  It remains atomic
        # on SQLite, where select_for_update() is deliberately a no-op, while
        # the Courier lock serializes this with regeneration on PostgreSQL.
        consumed = CourierLoginClaim.objects.filter(
            pk=candidate['pk'],
            courier_id=courier.pk,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=now,
        ).update(consumed_at=now)
        if consumed != 1:
            return None
        return courier


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
    _require_courier_identity(courier)
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
        courier = _lock_courier(courier)
        issued, _row = _issue_session(
            courier, ip_address=ip_address, user_agent=user_agent,
        )
    return issued


def _revoke_family(family_id, *, at=None):
    """Revoke a refresh family after its Courier root row has been locked."""
    at = at or timezone.now()
    rows = list(
        CourierRefreshToken.objects.select_for_update()
        .filter(family_id=family_id)
        .order_by('pk')
    )
    session_ids = sorted({row.access_session_id for row in rows
                          if row.access_session_id})
    sessions = list(
        Session.objects.select_for_update()
        .filter(pk__in=session_ids)
        .order_by('pk')
    )
    CourierRefreshToken.objects.filter(
        pk__in=[row.pk for row in rows], revoked_at__isnull=True,
    ).update(revoked_at=at)
    for session in sessions:
        _delete_access_session(session)


def rotate_refresh_token(raw_refresh, *, ip_address, user_agent):
    """Rotate once; replay of an already-used token revokes the whole family."""
    if not raw_refresh or not raw_refresh.startswith(REFRESH_TOKEN_PREFIX):
        return None
    now = timezone.now()
    candidate = (CourierRefreshToken.objects
                 .filter(token_digest=_digest(raw_refresh))
                 .values('pk', 'courier_id')
                 .first())
    if candidate is None:
        return None
    with transaction.atomic():
        courier = _lock_courier(candidate['courier_id'])
        row = (CourierRefreshToken.objects.select_for_update()
               .filter(pk=candidate['pk'], courier_id=courier.pk)
               .first())
        if row is None:
            return None
        if row.used_at is not None:
            # Replay is evidence that the old credential may have been stolen.
            _revoke_family(row.family_id, at=now)
            return None
        if row.revoked_at is not None or row.expires_at <= now:
            return None
        user = courier.user
        if (
            user.role != User.RoleChoices.COURIER
            or user.is_deleted
            or getattr(user, 'status', User.UserStatus.ACTIVE)
            != User.UserStatus.ACTIVE
        ):
            _revoke_family(row.family_id, at=now)
            return None

        # This compare-and-swap is necessary even though PostgreSQL has the
        # Courier/row locks: local installations and tests can use SQLite.
        claimed = CourierRefreshToken.objects.filter(
            pk=row.pk,
            used_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=now,
        ).update(used_at=now)
        if claimed != 1:
            latest = CourierRefreshToken.objects.filter(pk=row.pk).first()
            if latest is not None and latest.used_at is not None:
                _revoke_family(latest.family_id, at=now)
            return None
        if row.access_session_id:
            old_session = (Session.objects.select_for_update()
                           .filter(pk=row.access_session_id).first())
            _delete_access_session(old_session)
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
    candidate = (CourierRefreshToken.objects
                 .filter(token_digest=_digest(raw_refresh))
                 .values('pk', 'courier_id')
                 .first())
    if candidate is None:
        return
    with transaction.atomic():
        courier = _lock_courier(candidate['courier_id'])
        row = (CourierRefreshToken.objects.select_for_update()
               .filter(pk=candidate['pk'], courier_id=courier.pk).first())
        if row is not None:
            _revoke_family(row.family_id)


def revoke_access_token(raw_access):
    """Revoke an access token and its refresh family, if one exists."""
    token_hash = SessionRepository.hash_token(raw_access)
    if not token_hash:
        return
    candidate = (Session.objects.filter(payload=token_hash)
                 .values('pk', 'user_id_id').first())
    if candidate is None:
        SessionRepository.invalidate_cache(raw_access)
        return
    courier_id = (Courier.objects.filter(user_id=candidate['user_id_id'])
                  .values_list('pk', flat=True).first())
    with transaction.atomic():
        if courier_id is not None:
            _lock_courier(courier_id)
        row = (CourierRefreshToken.objects.select_for_update()
               .filter(access_session_id=candidate['pk']).first())
        if row is not None:
            _revoke_family(row.family_id)
        else:
            session = (Session.objects.select_for_update()
                       .filter(pk=candidate['pk'], payload=token_hash).first())
            if session is None:
                SessionRepository.invalidate_cache(raw_access)
                return
            _delete_access_session(session)


def revoke_all_for_courier(courier):
    """Revoke all mobile sessions, used when a manager resets a password."""
    with transaction.atomic():
        courier = _lock_courier(courier)
        family_ids = sorted({
            family_id for family_id in
            CourierRefreshToken.objects.filter(courier=courier)
            .values_list('family_id', flat=True)
        }, key=str)
        for family_id in family_ids:
            _revoke_family(family_id)
        # Also cover legacy sessions issued before refresh records existed.
        legacy = (Session.objects.select_for_update()
                  .filter(user_id=courier.user)
                  .order_by('pk'))
        for session in list(legacy):
            _delete_access_session(session)
