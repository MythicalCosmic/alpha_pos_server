from django.db import migrations
from django.db.models import F
from django.db.models.functions import Now


def backfill_dispatched_bot_orders(apps, schema_editor):
    """Tag already-dispatched SmartFood orders when the origin field arrives.

    BotOrder.pos_order is durable server evidence of the producer, so this
    backfill does not guess from notes, users, or order types.
    """
    Order = apps.get_model('base', 'Order')
    # Publish a real revision. A non-NULL cursor is important here: historical
    # migration models cannot call SyncMixin's on_commit publisher, and NULL
    # rows are intentionally replayed by the safety lane on *every* pull.
    # Stamping the migration transaction's DB time makes the revision visible
    # exactly like an ordinary committed cloud update without permanent replay.
    Order.objects.filter(
        bot_order__isnull=False,
    ).exclude(order_origin='TELEGRAM').update(
        order_origin='TELEGRAM',
        sync_version=F('sync_version') + 1,
        synced_at=Now(),
        updated_at=Now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0050_order_origin'),
        ('smartfood', '0002_reward_redemption_loyaltytransaction'),
    ]

    operations = [
        migrations.RunPython(
            backfill_dispatched_bot_orders,
            migrations.RunPython.noop,
        ),
    ]
