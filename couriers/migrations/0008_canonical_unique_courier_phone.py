from django.db import migrations, models


def _canonical_phone(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('00'):
        digits = digits[2:]
    if len(digits) == 9:
        digits = '998' + digits
    elif len(digits) == 10 and digits.startswith('0'):
        digits = '998' + digits[1:]
    return digits


def canonicalize_courier_phones(apps, schema_editor):
    Courier = apps.get_model('couriers', 'Courier')
    resolved = {}
    invalid = []
    duplicates = {}
    for courier_id, raw_phone in Courier.objects.order_by('pk').values_list(
        'pk', 'phone',
    ):
        phone = _canonical_phone(raw_phone)
        if len(phone) != 12 or not phone.startswith('998'):
            invalid.append((courier_id, raw_phone))
            continue
        if phone in resolved:
            duplicates.setdefault(phone, [resolved[phone]]).append(courier_id)
        else:
            resolved[phone] = courier_id

    if invalid or duplicates:
        raise RuntimeError(
            'Courier phone migration requires operator cleanup; '
            f'invalid={invalid!r}, duplicate_canonical={duplicates!r}'
        )

    for phone, courier_id in resolved.items():
        Courier.objects.filter(pk=courier_id).update(phone=phone)


class Migration(migrations.Migration):

    dependencies = [
        ('couriers', '0007_isolate_courier_identity'),
    ]

    operations = [
        migrations.RunPython(
            canonicalize_courier_phones,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='courier',
            name='phone',
            field=models.CharField(max_length=12, unique=True),
        ),
    ]
