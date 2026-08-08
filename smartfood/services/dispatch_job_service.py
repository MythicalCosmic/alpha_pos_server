"""Crash-recoverable Smart Food order dispatch outbox and retry policy."""

import logging
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from smartfood.models import BotOrder, BotOrderDispatchJob


logger = logging.getLogger(__name__)


def _positive_setting(name, default):
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


class DispatchJobService:
    @classmethod
    def ensure_pending_jobs(cls):
        """Backfill jobs for pending orders when automatic dispatch is enabled."""
        if not getattr(settings, 'SMARTFOOD_AUTO_DISPATCH', True):
            return 0
        now = timezone.now()
        order_ids = list(
            BotOrder.objects.filter(
                status=BotOrder.Status.PENDING,
                dispatch_job__isnull=True,
            ).values_list('id', flat=True)[:500]
        )
        if not order_ids:
            return 0
        jobs = [
            BotOrderDispatchJob(bot_order_id=order_id, next_attempt_at=now)
            for order_id in order_ids
        ]
        created = BotOrderDispatchJob.objects.bulk_create(
            jobs,
            ignore_conflicts=True,
        )
        return len(created)

    @classmethod
    def process_due(cls, limit=50):
        if not getattr(settings, 'SMARTFOOD_AUTO_DISPATCH', True):
            return {'claimed': 0, 'completed': 0}
        cls.ensure_pending_jobs()
        now = timezone.now()
        stale_before = now - timedelta(
            seconds=_positive_setting('SMARTFOOD_DISPATCH_LEASE_SECONDS', 60),
        )
        job_ids = list(
            BotOrderDispatchJob.objects.filter(
                Q(
                    status=BotOrderDispatchJob.Status.PENDING,
                    next_attempt_at__lte=now,
                )
                | Q(
                    status=BotOrderDispatchJob.Status.PROCESSING,
                    locked_at__lte=stale_before,
                )
            ).order_by('next_attempt_at', 'id')
            .values_list('id', flat=True)[:max(1, min(int(limit), 500))]
        )
        completed = sum(bool(cls.process(job_id)) for job_id in job_ids)
        return {'claimed': len(job_ids), 'completed': completed}

    @classmethod
    def process(cls, job_id, *, force=False):
        job = cls._claim(job_id, force=force)
        if job is None:
            return False
        claim_token = job.claim_token

        order = BotOrder.objects.filter(id=job.bot_order_id).first()
        if order is None or order.status != BotOrder.Status.PENDING:
            return cls._finish(
                job.id,
                claim_token,
                'Order was already handled',
            )

        from base.services.presence import resolve_active_cashier
        resolved = resolve_active_cashier()
        if not resolved:
            return cls._failed(
                job.id,
                claim_token,
                'No connected on-shift POS terminal is available',
            )

        try:
            from smartfood.services.dispatch_service import DispatchService
            result, status = DispatchService.dispatch(
                order.id,
                resolved['cashier_id'],
            )
        except Exception as exc:
            logger.exception('durable Smart Food dispatch raised (job=%s)', job.id)
            return cls._failed(job.id, claim_token, f'Dispatch raised: {exc}')

        order.refresh_from_db(fields=['status', 'pos_order_id'])
        if order.status != BotOrder.Status.PENDING:
            return cls._finish(job.id, claim_token, '')

        message = ''
        if isinstance(result, dict):
            message = str(result.get('message') or result.get('code') or '')
        return cls._failed(
            job.id,
            claim_token,
            message or f'Dispatch returned HTTP {status}',
        )

    @classmethod
    @transaction.atomic
    def _claim(cls, job_id, *, force):
        now = timezone.now()
        stale_before = now - timedelta(
            seconds=_positive_setting('SMARTFOOD_DISPATCH_LEASE_SECONDS', 60),
        )
        eligibility = Q(
            status=BotOrderDispatchJob.Status.PROCESSING,
            locked_at__lte=stale_before,
        )
        if force:
            eligibility |= Q(status=BotOrderDispatchJob.Status.PENDING)
        else:
            eligibility |= Q(
                status=BotOrderDispatchJob.Status.PENDING,
                next_attempt_at__lte=now,
            )
        job = (BotOrderDispatchJob.objects.select_for_update()
               .filter(id=job_id).filter(eligibility).first())
        if job is None:
            return None
        job.status = BotOrderDispatchJob.Status.PROCESSING
        job.attempts += 1
        job.locked_at = now
        job.claim_token = uuid4()
        job.last_error = ''
        job.save(update_fields=[
            'status', 'attempts', 'locked_at', 'claim_token', 'last_error',
            'updated_at',
        ])
        return job

    @classmethod
    @transaction.atomic
    def _finish(cls, job_id, claim_token, message):
        job = (BotOrderDispatchJob.objects.select_for_update()
               .filter(
                   id=job_id,
                   status=BotOrderDispatchJob.Status.PROCESSING,
                   claim_token=claim_token,
               ).first())
        if job is None:
            return False
        job.status = BotOrderDispatchJob.Status.DONE
        job.locked_at = None
        job.claim_token = None
        job.finished_at = timezone.now()
        job.last_error = str(message or '')[:500]
        job.save(update_fields=[
            'status', 'locked_at', 'claim_token', 'finished_at', 'last_error',
            'updated_at',
        ])
        return True

    @classmethod
    def _failed(cls, job_id, claim_token, error):
        job = BotOrderDispatchJob.objects.filter(
            id=job_id,
            status=BotOrderDispatchJob.Status.PROCESSING,
            claim_token=claim_token,
        ).first()
        if job is None:
            return False
        max_attempts = _positive_setting('SMARTFOOD_DISPATCH_MAX_ATTEMPTS', 12)
        if job.attempts >= max_attempts:
            order = (BotOrder.objects.select_related('customer')
                     .filter(id=job.bot_order_id).first())
            if order is None or order.status != BotOrder.Status.PENDING:
                return cls._finish(job_id, claim_token, error)
            from smartfood.services.dispatch_service import DispatchService
            from smartfood.services.notification_service import technical_rejection_reason
            reason = technical_rejection_reason(order.customer)
            try:
                DispatchService.reject(order.id, reason=reason)
            except Exception:
                logger.exception('terminal Smart Food rejection failed (job=%s)', job_id)
                return cls._reschedule(job_id, claim_token, error)
            order.refresh_from_db(fields=['status'])
            if order.status != BotOrder.Status.PENDING:
                return cls._finish(job_id, claim_token, error)
        return cls._reschedule(job_id, claim_token, error)

    @classmethod
    @transaction.atomic
    def _reschedule(cls, job_id, claim_token, error):
        job = (BotOrderDispatchJob.objects.select_for_update()
               .filter(
                   id=job_id,
                   status=BotOrderDispatchJob.Status.PROCESSING,
                   claim_token=claim_token,
               ).first())
        if job is None:
            return False
        base = _positive_setting('SMARTFOOD_DISPATCH_RETRY_BASE_SECONDS', 5)
        ceiling = _positive_setting('SMARTFOOD_DISPATCH_RETRY_MAX_SECONDS', 60)
        delay = min(ceiling, base * (2 ** min(max(job.attempts - 1, 0), 6)))
        job.status = BotOrderDispatchJob.Status.PENDING
        job.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        job.locked_at = None
        job.claim_token = None
        job.last_error = str(error or 'Dispatch failed')[:500]
        job.save(update_fields=[
            'status', 'next_attempt_at', 'locked_at', 'claim_token',
            'last_error', 'updated_at',
        ])
        return False
