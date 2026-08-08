import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('smartfood', '0003_backfill_telegram_order_origin'),
    ]

    operations = [
        migrations.AddField(
            model_name='botorder',
            name='client_order_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='botorder',
            name='request_fingerprint',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='botorder',
            name='loyalty_earned_settled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='botorder',
            name='loyalty_spend_restored_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='botorder',
            name='loyalty_earn_reversed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='botorder',
            constraint=models.UniqueConstraint(
                condition=models.Q(client_order_id__isnull=False),
                fields=('customer', 'client_order_id'),
                name='sf_order_customer_client_id_uniq',
            ),
        ),
        migrations.CreateModel(
            name='BotOrderDispatchJob',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(
                    choices=[
                        ('PENDING', 'Pending'),
                        ('PROCESSING', 'Processing'),
                        ('DONE', 'Done'),
                    ],
                    db_index=True,
                    default='PENDING',
                    max_length=12,
                )),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(
                    db_index=True,
                    default=django.utils.timezone.now,
                )),
                ('locked_at', models.DateTimeField(blank=True, null=True)),
                ('claim_token', models.UUIDField(
                    blank=True,
                    editable=False,
                    null=True,
                )),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.CharField(
                    blank=True,
                    default='',
                    max_length=500,
                )),
                ('bot_order', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dispatch_job',
                    to='smartfood.botorder',
                )),
            ],
            options={
                'ordering': ['next_attempt_at', 'id'],
                'indexes': [
                    models.Index(
                        fields=['status', 'next_attempt_at'],
                        name='sf_dispatch_status_next_idx',
                    ),
                ],
            },
        ),
    ]
