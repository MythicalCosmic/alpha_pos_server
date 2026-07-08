import logging

from base.helpers.response import ServiceResponse
from base.repositories.app_settings import AppSettingsRepository

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
            'waiter_enabled': settings.waiter_enabled,
            # Operating-day cutover as "HH:MM" (e.g. "03:00") — the FE uses it to
            # compute business dates for its date-preset chips.
            'business_day_start': (
                settings.business_day_start.strftime('%H:%M')
                if settings.business_day_start else '03:00'
            ),
            # Working hours the venue trades — the FE's "Working hours" preset for
            # the tod_from/tod_to dashboard filter.
            'business_open': (
                settings.business_open.strftime('%H:%M')
                if settings.business_open else '09:00'
            ),
            'business_close': (
                settings.business_close.strftime('%H:%M')
                if settings.business_close else '23:00'
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
    def update(**kwargs):
        settings = AppSettingsRepository.load()

        app_fields = {'hr_enabled', 'waiter_enabled'}
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
