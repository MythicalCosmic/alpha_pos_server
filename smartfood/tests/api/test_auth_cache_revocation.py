from datetime import timedelta
from threading import Event, Thread
from urllib.parse import urlencode

import pytest
from django.db import close_old_connections
from django.db.models import QuerySet
from django.utils import timezone

from smartfood.models import Customer, CustomerSession
from smartfood.repositories import CustomerSessionRepository
from smartfood.security import verify_init_data

pytestmark = pytest.mark.django_db(transaction=True)
TOKEN = 'customer-cache-revocation-test'


def _session():
    customer = Customer.objects.create(telegram_id=998877001, first_name='Cache')
    session = CustomerSession.objects.create(
        customer=customer, payload=CustomerSessionRepository.hash_token(TOKEN),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return customer, session


def _revoke(customer, session, operation):
    if operation == 'block':
        customer.is_blocked = True
        customer.save(update_fields=['is_blocked'])
    elif operation == 'expiry':
        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=['expires_at'])
    else:
        session.delete()


def _assert_revoked(operation):
    current = CustomerSessionRepository.get_by_token(TOKEN)
    if operation == 'delete':
        assert current is None
    elif operation == 'expiry':
        assert current.is_expired()
    else:
        assert current.customer.is_blocked


@pytest.mark.parametrize('operation', ['block', 'delete', 'expiry'])
def test_saved_revocation_clears_customer_auth_cache(operation):
    customer, session = _session()
    assert CustomerSessionRepository.get_by_token(TOKEN) is not None
    _revoke(customer, session, operation)
    _assert_revoked(operation)


@pytest.mark.parametrize('operation', ['block', 'delete'])
def test_late_customer_session_read_cannot_restore_revoked_access(operation, monkeypatch):
    customer, session = _session()
    read, finish = Event(), Event()
    errors = []
    original_first = QuerySet.first

    def held_first(qs):
        result = original_first(qs)
        if qs.model is CustomerSession and result is not None:
            read.set()
            assert finish.wait(10)
        return result

    def reader():
        close_old_connections()
        try:
            CustomerSessionRepository.get_by_token(TOKEN)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    monkeypatch.setattr(QuerySet, 'first', held_first)
    thread = Thread(target=reader)
    thread.start()
    try:
        assert read.wait(10)
        _revoke(customer, session, operation)
    finally:
        finish.set()
        thread.join(15)
        monkeypatch.setattr(QuerySet, 'first', original_first)
    assert not thread.is_alive() and not errors
    _assert_revoked(operation)


def test_non_ascii_init_data_hash_is_rejected_without_server_error():
    assert verify_init_data(urlencode({'hash': 'é' * 64}), bot_token='test-token') is None
