import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _pending_order(customer, product, *, address_text='Amir Temur 12'):
    from smartfood.models import BotOrder, BotOrderItem

    order = BotOrder.objects.create(
        customer=customer,
        client_order_id=uuid4(),
        request_fingerprint=uuid4().hex,
        status=BotOrder.Status.PENDING,
        order_type=BotOrder.OrderType.DELIVERY,
        address_text=address_text,
        phone_number=customer.phone_number,
        subtotal=Decimal('39000.00'),
        total=Decimal('39000.00'),
        payment_method=BotOrder.Payment.CASH,
    )
    BotOrderItem.objects.create(
        bot_order=order,
        product=product,
        quantity=1,
        unit_price=Decimal('39000.00'),
        line_total=Decimal('39000.00'),
    )
    return order


def _mark_till_live(cashier, active_shift):
    from base.services import presence

    presence.mark_device_live(
        active_shift.device_id,
        active_shift.branch_id,
        cashier.id,
    )


class TestDurableRetry:
    def test_transient_exception_reschedules_then_dispatches_exactly_once(
        self,
        monkeypatch,
        settings,
        cfg,
        active_shift,
        cashier,
        product,
        customer,
    ):
        from base.models import Order
        from smartfood.models import BotOrder, BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService
        from smartfood.services.dispatch_service import DispatchService

        settings.DEPLOYMENT_MODE = 'cloud'
        settings.SMARTFOOD_AUTO_DISPATCH = True
        settings.SMARTFOOD_DISPATCH_MAX_ATTEMPTS = 3
        settings.SMARTFOOD_DISPATCH_RETRY_BASE_SECONDS = 1
        settings.SMARTFOOD_DISPATCH_RETRY_MAX_SECONDS = 2
        settings.CUSTOMER_BOT_TOKEN = ''
        _mark_till_live(cashier, active_shift)
        bot_order = _pending_order(customer, product)
        job = BotOrderDispatchJob.objects.create(bot_order=bot_order)

        real_dispatch = DispatchService.dispatch
        calls = []

        def flaky_dispatch(bot_order_id, cashier_id, operator=None):
            calls.append(bot_order_id)
            if len(calls) == 1:
                raise RuntimeError('temporary database disconnect')
            return real_dispatch(bot_order_id, cashier_id, operator=operator)

        monkeypatch.setattr(DispatchService, 'dispatch', flaky_dispatch)

        assert DispatchJobService.process(job.id, force=True) is False
        job.refresh_from_db()
        bot_order.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.PENDING
        assert job.attempts == 1
        assert job.next_attempt_at > timezone.now()
        assert 'temporary database disconnect' in job.last_error
        assert bot_order.status == BotOrder.Status.PENDING
        assert bot_order.pos_order_id is None
        assert Order.objects.filter(bot_order__isnull=False).count() == 0

        BotOrderDispatchJob.objects.filter(pk=job.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        result = DispatchJobService.process_due(limit=10)
        assert result == {'claimed': 1, 'completed': 1}
        job.refresh_from_db()
        bot_order.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.DONE
        assert job.attempts == 2
        assert job.finished_at is not None
        assert bot_order.status == BotOrder.Status.DISPATCHED
        assert bot_order.pos_order_id is not None
        assert Order.objects.filter(pk=bot_order.pos_order_id).count() == 1

        # Worker duplication/replay after success is a no-op, not a second POS
        # order or another dispatch invocation.
        assert DispatchJobService.process(job.id, force=True) is False
        assert DispatchJobService.process_due(limit=10) == {
            'claimed': 0,
            'completed': 0,
        }
        assert len(calls) == 2
        assert Order.objects.filter(bot_order__isnull=False).count() == 1

    def test_no_terminal_is_retryable_and_does_not_immediately_reject_customer(
        self,
        settings,
        cfg,
        active_shift,
        product,
        customer,
    ):
        from smartfood.models import BotOrder, BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService

        settings.SMARTFOOD_AUTO_DISPATCH = True
        settings.SMARTFOOD_DISPATCH_MAX_ATTEMPTS = 3
        cache.clear()
        bot_order = _pending_order(customer, product)
        job = BotOrderDispatchJob.objects.create(bot_order=bot_order)

        assert DispatchJobService.process(job.id, force=True) is False

        job.refresh_from_db()
        bot_order.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.PENDING
        assert job.attempts == 1
        assert 'connected on-shift POS' in job.last_error
        assert bot_order.status == BotOrder.Status.PENDING
        assert bot_order.reject_reason == ''

    def test_sweeper_backfills_missing_job_and_processes_it(
        self,
        settings,
        cfg,
        active_shift,
        cashier,
        product,
        customer,
    ):
        from smartfood.models import BotOrder, BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService

        settings.DEPLOYMENT_MODE = 'cloud'
        settings.SMARTFOOD_AUTO_DISPATCH = True
        settings.CUSTOMER_BOT_TOKEN = ''
        _mark_till_live(cashier, active_shift)
        bot_order = _pending_order(customer, product)
        assert not BotOrderDispatchJob.objects.filter(bot_order=bot_order).exists()

        assert DispatchJobService.ensure_pending_jobs() == 1
        assert DispatchJobService.ensure_pending_jobs() == 0
        assert BotOrderDispatchJob.objects.filter(bot_order=bot_order).count() == 1

        result = DispatchJobService.process_due(limit=10)
        assert result == {'claimed': 1, 'completed': 1}
        bot_order.refresh_from_db()
        assert bot_order.status == BotOrder.Status.DISPATCHED

    def test_fresh_processing_lease_is_fenced_and_stale_lease_is_recovered(
        self,
        settings,
        cfg,
        active_shift,
        cashier,
        product,
        customer,
    ):
        from smartfood.models import BotOrder, BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService

        settings.DEPLOYMENT_MODE = 'cloud'
        settings.SMARTFOOD_AUTO_DISPATCH = True
        settings.SMARTFOOD_DISPATCH_LEASE_SECONDS = 60
        settings.CUSTOMER_BOT_TOKEN = ''
        _mark_till_live(cashier, active_shift)
        bot_order = _pending_order(customer, product)
        job = BotOrderDispatchJob.objects.create(
            bot_order=bot_order,
            status=BotOrderDispatchJob.Status.PROCESSING,
            attempts=1,
            locked_at=timezone.now(),
        )

        assert DispatchJobService.process(job.id, force=True) is False
        job.refresh_from_db()
        assert job.attempts == 1

        BotOrderDispatchJob.objects.filter(pk=job.pk).update(
            locked_at=timezone.now() - timedelta(seconds=61),
        )
        assert DispatchJobService.process_due(limit=10) == {
            'claimed': 1,
            'completed': 1,
        }
        job.refresh_from_db()
        bot_order.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.DONE
        assert job.attempts == 2
        assert bot_order.status == BotOrder.Status.DISPATCHED

    def test_one_to_one_job_constraint_prevents_duplicate_workers_from_enqueuing(
        self,
        customer,
        product,
    ):
        from smartfood.models import BotOrderDispatchJob

        bot_order = _pending_order(customer, product)
        BotOrderDispatchJob.objects.create(bot_order=bot_order)

        with pytest.raises(IntegrityError), transaction.atomic():
            BotOrderDispatchJob.objects.create(bot_order=bot_order)

    def test_stale_worker_cannot_finish_or_reschedule_a_newer_claim(
        self,
        settings,
        customer,
        product,
    ):
        from smartfood.models import BotOrderDispatchJob
        from smartfood.services.dispatch_job_service import DispatchJobService

        settings.SMARTFOOD_DISPATCH_LEASE_SECONDS = 30
        bot_order = _pending_order(customer, product)
        job = BotOrderDispatchJob.objects.create(bot_order=bot_order)

        first = DispatchJobService._claim(job.id, force=True)
        first_token = first.claim_token
        BotOrderDispatchJob.objects.filter(pk=job.pk).update(
            locked_at=timezone.now() - timedelta(seconds=31),
        )
        second = DispatchJobService._claim(job.id, force=False)
        second_token = second.claim_token
        assert second_token != first_token

        assert DispatchJobService._finish(
            job.id,
            first_token,
            'stale completion',
        ) is False
        assert DispatchJobService._reschedule(
            job.id,
            first_token,
            'stale failure',
        ) is False

        job.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.PROCESSING
        assert job.claim_token == second_token
        assert job.attempts == 2
        assert job.last_error == ''

        assert DispatchJobService._finish(
            job.id,
            second_token,
            'current completion',
        ) is True
        job.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.DONE
        assert job.claim_token is None
        assert job.last_error == 'current completion'

    def test_http_replay_does_not_bypass_dispatch_backoff(
        self,
        settings,
        auth_client,
        cfg,
        active_shift,
        cashier,
        product,
        address,
    ):
        from smartfood.models import BotOrder, BotOrderDispatchJob

        settings.SMARTFOOD_AUTO_DISPATCH = True
        _mark_till_live(cashier, active_shift)
        payload = {
            'client_order_id': str(uuid4()),
            'items': [{'product_id': product.id, 'quantity': 1}],
            'order_type': 'DELIVERY',
            'address_id': address.id,
            'payment_method': 'CASH',
        }
        first = auth_client.post(
            '/api/smartfood/orders',
            data=json.dumps(payload),
            content_type='application/json',
        )
        assert first.status_code == 201, first.content
        bot_order = BotOrder.objects.get(pk=first.json()['data']['id'])
        job = BotOrderDispatchJob.objects.get(bot_order=bot_order)
        future = timezone.now() + timedelta(minutes=5)
        BotOrderDispatchJob.objects.filter(pk=job.pk).update(
            next_attempt_at=future,
            attempts=4,
            last_error='waiting for scheduled retry',
        )

        replay = auth_client.post(
            '/api/smartfood/orders',
            data=json.dumps(payload),
            content_type='application/json',
        )
        assert replay.status_code == 200, replay.content
        job.refresh_from_db()
        bot_order.refresh_from_db()
        assert job.status == BotOrderDispatchJob.Status.PENDING
        assert job.attempts == 4
        assert job.next_attempt_at == future
        assert job.last_error == 'waiting for scheduled retry'
        assert bot_order.status == BotOrder.Status.PENDING


@pytest.mark.django_db(transaction=True)
def test_terminal_failure_rejects_politely_and_notifies_telegram_once(
    monkeypatch,
    settings,
    bot_token,
    cfg,
    active_shift,
    product,
    customer,
):
    from smartfood.models import BotOrder, BotOrderDispatchJob
    from smartfood.services.dispatch_job_service import DispatchJobService

    settings.SMARTFOOD_AUTO_DISPATCH = True
    settings.SMARTFOOD_DISPATCH_MAX_ATTEMPTS = 1
    cache.clear()
    cfg.support_phone = ''
    cfg.support_telegram = '@smartfood_help'
    cfg.support_email = ''
    cfg.save()
    customer.language = 'en'
    customer.save(update_fields=['language'])
    sent = []

    class TelegramResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'ok': True}

    def fake_post(url, json, timeout):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return TelegramResponse()

    monkeypatch.setattr(
        'smartfood.services.notification_service.requests.post',
        fake_post,
    )
    bot_order = _pending_order(customer, product)
    job = BotOrderDispatchJob.objects.create(bot_order=bot_order)

    assert DispatchJobService.process(job.id, force=True) is True

    job.refresh_from_db()
    bot_order.refresh_from_db()
    assert job.status == BotOrderDispatchJob.Status.DONE
    assert job.attempts == 1
    assert 'connected on-shift POS' in job.last_error
    assert bot_order.status == BotOrder.Status.REJECTED
    assert 'technical problem' in bot_order.reject_reason
    assert 'Telegram @smartfood_help' in bot_order.reject_reason
    assert 'connected on-shift POS' not in bot_order.reject_reason
    assert len(sent) == 1
    assert sent[0]['json']['chat_id'] == customer.telegram_id
    assert bot_order.reject_reason in sent[0]['json']['text']
    assert sent[0]['timeout'] == 10

    assert DispatchJobService.process(job.id, force=True) is False
    assert len(sent) == 1
