"""Thin ORM helpers. CustomerSessionRepository mirrors base.SessionRepository:
the raw bearer token is never stored — only its SHA-256 digest (in
CustomerSession.payload), with a short cache in front of the lookup."""
import hashlib

from django.conf import settings
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
        cache_key = _CACHE_PREFIX + token_hash
        ttl = getattr(settings, 'SESSION_CACHE_TTL', 300)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        session = cls.model.objects.select_related('customer').filter(payload=token_hash).first()
        if session:
            cache.set(cache_key, session, ttl)
        return session

    @classmethod
    def invalidate(cls, token):
        h = cls.hash_token(token)
        if h:
            cache.delete(_CACHE_PREFIX + h)
