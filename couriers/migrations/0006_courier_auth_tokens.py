import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0049_reporting_window_defaults'),
        ('couriers', '0005_backfill_paid_order_accounting_cursor'),
    ]

    operations = [
        migrations.CreateModel(
            name='CourierLoginClaim',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID',
                )),
                ('token_digest', models.CharField(max_length=64, unique=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('courier', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='login_claims', to='couriers.courier',
                )),
                ('issued_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to='base.user',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='CourierRefreshToken',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID',
                )),
                ('token_digest', models.CharField(max_length=64, unique=True)),
                ('family_id', models.UUIDField(
                    db_index=True, default=uuid.uuid4, editable=False,
                )),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('access_session', models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='courier_refresh_token', to='base.session',
                )),
                ('courier', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='refresh_tokens', to='couriers.courier',
                )),
                ('replaced_by', models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='replaces', to='couriers.courierrefreshtoken',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='courierloginclaim',
            index=models.Index(
                fields=['courier', 'expires_at'], name='courier_qr_expiry_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='courierrefreshtoken',
            index=models.Index(
                fields=['courier', 'revoked_at'],
                name='courier_refresh_active_idx',
            ),
        ),
    ]
