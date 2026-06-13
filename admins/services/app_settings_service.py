import logging

from base.helpers.response import ServiceResponse
from base.repositories.app_settings import AppSettingsRepository

logger = logging.getLogger(__name__)


class AppSettingsService:

    @staticmethod
    def get_all():
        settings = AppSettingsRepository.load()

        data = {
            'hr_enabled': settings.hr_enabled,
            'waiter_enabled': settings.waiter_enabled,
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
