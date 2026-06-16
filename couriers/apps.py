from django.apps import AppConfig


class CouriersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'couriers'
    verbose_name = 'Courier delivery'

    def ready(self):
        # Wire the kitchen READY -> order.ready courier notification signal.
        from couriers import signals  # noqa: F401
