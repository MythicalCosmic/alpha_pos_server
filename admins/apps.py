from django.apps import AppConfig


class AdminsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'admins'

    def ready(self):
        from admins import signals  # noqa: F401 — registers the owner-app push hooks
