import logging

from base.helpers.response import ServiceResponse
from base.repositories.app_settings import AppSettingsRepository
from base.models import AppSettings
from django.db import transaction
from base.services.waiter_settings import policy_payload

logger = logging.getLogger(__name__)


class AppSettingsService:

    @staticmethod
    def _parse_time(value):
        """Accept a "HH:MM" / "HH:MM:SS" string (or a time) -> datetime.time, else None."""
        from datetime import time, datetime as _dt
        if isinstance(value, time):
            return value
        if not isinstance(value, str):
            return None
        for fmt in ('%H:%M', '%H:%M:%S'):
            try:
                return _dt.strptime(value.strip(), fmt).time()
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def get_all():
        settings = AppSettingsRepository.load()

        data = {
            'hr_enabled': settings.hr_enabled,
            **policy_payload(settings),
            'waiter_policy_scope': 'this_backend',
            # Canonical operating-day opening. Reporting itself deliberately
            # enforces 07:00 -> next-day 03:00 independently of this display
            # setting, so an old database row cannot change money membership.
            'business_day_start': (
                settings.business_day_start.strftime('%H:%M')
                if settings.business_day_start else '07:00'
            ),
            # Working-hour defaults exposed to the FE.
            'business_open': (
                settings.business_open.strftime('%H:%M')
                if settings.business_open else '07:00'
            ),
            'business_close': (
                settings.business_close.strftime('%H:%M')
                if settings.business_close else '03:00'
            ),
        }

        try:
            from stock.services import StockSettingsService
            stock_settings = StockSettingsService.load()
            data['stock_enabled'] = stock_settings.stock_enabled
        except Exception:
            data['stock_enabled'] = False

        return ServiceResponse.success(data={'settings': data})

    @staticmethod
    @transaction.atomic
    def update(**kwargs):
        AppSettingsRepository.load()
        settings = AppSettings.objects.select_for_update().get(pk=1)
        for field in ('hr_enabled', 'stock_enabled', 'waiter_enabled', 'waiter_require_shift'):
            if field in kwargs and type(kwargs[field]) is not bool:
                return ServiceResponse.validation_error({field: 'Use true or false.'})
        if ('waiter_payment_mode' in kwargs and
                kwargs['waiter_payment_mode'] not in AppSettings.WaiterPaymentMode.values):
            return ServiceResponse.validation_error({'waiter_payment_mode': 'Choose a supported payment mode.'})

        app_fields = {'hr_enabled', 'waiter_enabled', 'waiter_payment_mode', 'waiter_require_shift'}
        stock_fields = {'stock_enabled'}

        for key, value in kwargs.items():
            if key in app_fields:
                setattr(settings, key, value)

        if 'business_day_start' in kwargs:
            parsed = AppSettingsService._parse_time(kwargs['business_day_start'])
            if parsed is None:
                return ServiceResponse.validation_error(
                    errors={'business_day_start': 'Must be a time string "HH:MM" or "HH:MM:SS"'},
                    message='Invalid business_day_start',
                )
            settings.business_day_start = parsed

        for _hh in ('business_open', 'business_close'):
            if _hh in kwargs:
                parsed = AppSettingsService._parse_time(kwargs[_hh])
                if parsed is None:
                    return ServiceResponse.validation_error(
                        errors={_hh: 'Must be a time string "HH:MM" or "HH:MM:SS"'},
                        message=f'Invalid {_hh}',
                    )
                setattr(settings, _hh, parsed)

        settings.save()

        if stock_fields & set(kwargs.keys()):
            try:
                from stock.services import StockSettingsService
                stock_settings = StockSettingsService.load()
                if 'stock_enabled' in kwargs:
                    stock_settings.stock_enabled = kwargs['stock_enabled']
                    stock_settings.save()
            except Exception:
                logger.exception('failed to mirror stock_enabled to StockSettings')

        return AppSettingsService.get_all()

    @staticmethod
    def toggle(app_name, enabled):
        valid_apps = {'hr': 'hr_enabled', 'waiter': 'waiter_enabled', 'stock': 'stock_enabled'}

        if app_name not in valid_apps:
            return ServiceResponse.validation_error(
                errors={'app_name': f'Must be one of: {", ".join(valid_apps.keys())}'},
                message='Invalid app name',
            )

        field = valid_apps[app_name]
        return AppSettingsService.update(**{field: enabled})
