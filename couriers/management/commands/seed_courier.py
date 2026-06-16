"""Seed one courier (+ login) and a demo delivery order with an assignment, so
the mobile app's feeds return real data (spec §9 checklist).

    python manage.py seed_courier
    python manage.py seed_courier --phone "+998901234567" --password pass123

Idempotent: re-running updates the same courier (matched by --code).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from base.models import User, Order, OrderItem, Product
from base.security.hashing import hash_password

from couriers.models import Courier, DeliveryAssignment


class Command(BaseCommand):
    help = 'Seed a demo courier + a delivery order with an assignment.'

    def add_arguments(self, parser):
        parser.add_argument('--code', default='CR-118')
        parser.add_argument('--phone', default='+998901234567')
        parser.add_argument('--password', default='courier123')
        parser.add_argument('--email', default='courier.demo@alpha.local')

    def handle(self, *args, **o):
        user, created = User.objects.get_or_create(
            email=o['email'],
            defaults={'first_name': 'Jasur', 'last_name': 'Rakhimov',
                      'role': getattr(User.RoleChoices, 'CASHIER', 'CASHIER'),
                      'status': 'ACTIVE', 'password': hash_password(o['password'])},
        )
        if not created:
            user.password = hash_password(o['password'])
            user.save(update_fields=['password'])

        courier, _ = Courier.objects.update_or_create(
            code=o['code'],
            defaults={'user': user, 'first_name': 'Jasur', 'last_name': 'Rakhimov',
                      'phone': o['phone'], 'vehicle': 'Scooter', 'plate': '01 A 777 BC',
                      'branch_id': 'cloud', 'branch_name': 'Alpha — Chilonzor',
                      'rating': Decimal('4.9'), 'online': True, 'share_loc': True,
                      'shift_started_at': timezone.now()},
        )
        self.stdout.write(self.style.SUCCESS(
            f'Courier {courier.code}: phone={o["phone"]} password={o["password"]}'))

        # A demo delivery order (only if we can satisfy Order's required FKs).
        order = Order.objects.create(
            user=user, order_type='DELIVERY', status='PREPARING', branch_id='cloud',
            phone_number='+998934128801', subtotal=Decimal('113000'),
            total_amount=Decimal('113000'), is_paid=False,
        )
        product = Product.objects.first()
        if product:
            OrderItem.objects.create(order=order, product=product, quantity=1,
                                     price=Decimal('85000'), original_price=Decimal('85000'))
        DeliveryAssignment.objects.update_or_create(
            order=order,
            defaults={'courier': courier, 'step': DeliveryAssignment.Step.ASSIGNED,
                      'fee': 15000, 'assigned_at': timezone.now(),
                      'addr_text': "Bunyodkor ko'chasi 12, kv. 34",
                      'addr_landmark': 'near Chilonzor metro',
                      'addr_lat': 41.2853, 'addr_lng': 69.2034, 'distance_km': 2.4},
        )
        self.stdout.write(self.style.SUCCESS(f'Demo delivery order #{order.id} assigned.'))
