"""Thin ORM helpers. CustomerSessionRepository mirrors base.SessionRepository:
the raw bearer token is never stored — only its SHA-256 digest (in
CustomerSession.payload). Authorization always reads current database state."""
import hashlib

from django.core.cache import cache

from smartfood.models import CustomerSession

_CACHE_PREFIX = 'smartfood:session:'


class CustomerSessionRepository:
    model = CustomerSession

    @staticmethod
    def hash_token(token):
        if not token:
            return None
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @classmethod
    def get_by_token(cls, token):
        token_hash = cls.hash_token(token)
        if not token_hash:
            return None
        # A cached joined customer can survive blocking or session revocation,
        # including when an in-flight reader fills the cache after invalidation.
        # Read the indexed session and current customer together for each check.
        return cls.model.objects.select_related('customer').filter(payload=token_hash).first()

    @classmethod
    def invalidate(cls, token):
        h = cls.hash_token(token)
        if h:
            cache.delete(_CACHE_PREFIX + h)
