import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('smartfood', '0004_order_idempotency_dispatch_outbox'),
    ]

    operations = [
        migrations.AddField(
            model_name='botconfig',
            name='bot_token',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.CreateModel(
            name='BotVisit',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'client_visit_id',
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    'source',
                    models.CharField(
                        choices=[('MINI_APP', 'Telegram Mini App')],
                        default='MINI_APP',
                        max_length=16,
                    ),
                ),
                ('user_agent', models.CharField(blank=True, default='', max_length=256)),
                ('ip_address', models.CharField(blank=True, default='', max_length=45)),
                ('visited_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='visits',
                        to='smartfood.customer',
                    ),
                ),
            ],
            options={
                'ordering': ['-visited_at', '-id'],
                'indexes': [
                    models.Index(
                        fields=['customer', '-visited_at'],
                        name='sf_visit_customer_at_idx',
                    ),
                ],
            },
        ),
    ]
