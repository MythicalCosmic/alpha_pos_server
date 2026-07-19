from django.db import migrations, transaction
from django.db.models import F
from django.utils import timezone


def isolate_courier_users(apps, schema_editor):
    Courier = apps.get_model('couriers', 'Courier')
    Session = apps.get_model('base', 'Session')
    User = apps.get_model('base', 'User')

    user_ids = list(Courier.objects.values_list('user_id', flat=True))
    if not user_ids:
        return
    payloads = list(
        Session.objects.filter(user_id__in=user_ids)
        .exclude(payload='')
        .values_list('payload', flat=True)
    )
    # Existing courier sessions were cashier-audience sessions. Revoke them at
    # the role boundary so no cached bearer survives with the old privilege.
    Session.objects.filter(user_id__in=user_ids).delete()

    # QuerySet.update bypasses SyncMixin.save(), so explicitly advance the
    # version and publish one committed timestamp.  User is globally pulled;
    # without this bookkeeping an already-running till can retain CASHIER and
    # keep treating the identity as a POS operator indefinitely.
    published_at = timezone.now()
    User.objects.filter(pk__in=user_ids).exclude(role='COURIER').update(
        role='COURIER',
        sync_version=F('sync_version') + 1,
        synced_at=published_at,
        updated_at=published_at,
    )

    def invalidate_session_cache():
        try:
            from django.core.cache import cache
            for payload in payloads:
                cache.delete(f'session:{payload}')
        except Exception:
            # DB revocation is authoritative.  A cache outage after commit must
            # not make the already-applied migration appear to have failed.
            pass

    transaction.on_commit(
        invalidate_session_cache,
        using=schema_editor.connection.alias,
        robust=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0051_courier_role_and_payment_repair_audit'),
        ('couriers', '0006_courier_auth_tokens'),
    ]

    operations = [
        migrations.RunPython(
            isolate_courier_users,
            migrations.RunPython.noop,
        ),
    ]
