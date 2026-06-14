"""Shared fixtures for the smartfood test suite."""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone


@pytest.fixture(autouse=True)
def _clear_cache():
    # Sessions/config are cached by hash/key; clear so a rolled-back test row
    # can't leak a cached object into the next test.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def bot_token(settings):
    settings.CUSTOMER_BOT_TOKEN = '123456:TEST-BOT-TOKEN'
    return settings.CUSTOMER_BOT_TOKEN


@pytest.fixture
def cfg(db):
    from smartfood.models import BotConfig
    c = BotConfig.load()
    c.enabled = True
    c.delivery_fee = Decimal('12000')
    c.free_delivery_threshold = Decimal('100000')
    c.min_order_amount = Decimal('0')
    c.loyalty_earn_per = Decimal('1000')     # 1 point per 1000 UZS spent
    c.loyalty_point_value = Decimal('100')   # 1 point = 100 UZS at redeem
    c.save()
    return c


@pytest.fixture
def cashier(db):
    from base.models import User
    return User.objects.create(first_name='Cash', last_name='Ier', email='cashier@x.local',
                               role='CASHIER', status='ACTIVE', password='!')


@pytest.fixture
def active_shift(db, cashier):
    from base.models import Shift
    return Shift.objects.create(user=cashier, start_time=timezone.now() - timedelta(hours=1),
                                status='ACTIVE')


@pytest.fixture
def manager(db):
    from base.models import User
    return User.objects.create(first_name='Man', last_name='Ager', email='manager@x.local',
                               role='MANAGER', status='ACTIVE', password='!')


@pytest.fixture
def operator_client(db, manager):
    """A SEPARATE test client authenticated as a manager (operator) via base.Session.
    Own Client instance so it never clobbers auth_client's Bearer header."""
    from django.test import Client
    from base.models import Session
    from base.repositories import SessionRepository
    raw = secrets.token_hex(32)
    Session.objects.create(user_id=manager, payload=SessionRepository.hash_token(raw),
                           user_agent='', expires_at=timezone.now() + timedelta(hours=1))
    return Client(HTTP_AUTHORIZATION='Bearer ' + raw)


@pytest.fixture
def category(db):
    from base.models import Category
    from smartfood.models import BotCategory
    cat = Category.objects.create(name='Burgers', status='ACTIVE', slug='burgers')
    BotCategory.objects.create(category=cat, is_published=True, is_selling=True, name_en='Burgers')
    return cat


@pytest.fixture
def product(db, category):
    """A published burger at 39000 with a Large (+10000) size and Cheese(+6000)/
    Bacon(+8000) toppings (group 'Extras', optional, max 3). Handy attrs stashed
    on the instance for tests: ._large, ._group, ._cheese, ._bacon."""
    from base.models import Product
    from smartfood.models import BotProduct, Size, ToppingGroup, Topping
    p = Product.objects.create(name='Classic', price=Decimal('39000'), category=category)
    BotProduct.objects.create(product=p, is_published=True, is_selling=True, name_en='Classic Burger')
    Size.objects.create(product=p, name_en='Medium', price_delta=Decimal('0'), is_default=True)
    p._large = Size.objects.create(product=p, name_en='Large', price_delta=Decimal('10000'))
    p._group = ToppingGroup.objects.create(product=p, name_en='Extras', is_required=False, max_select=3)
    p._cheese = Topping.objects.create(group=p._group, name_en='Cheese', price=Decimal('6000'))
    p._bacon = Topping.objects.create(group=p._group, name_en='Bacon', price=Decimal('8000'))
    return p


@pytest.fixture
def customer(db):
    from smartfood.models import Customer
    return Customer.objects.create(telegram_id=777001, first_name='Aziz', language='uz',
                                   phone_number='+998901234567')


@pytest.fixture
def raw_token(db, customer):
    from smartfood.models import CustomerSession
    from smartfood.repositories import CustomerSessionRepository
    raw = secrets.token_hex(32)
    CustomerSession.objects.create(customer=customer,
                                   payload=CustomerSessionRepository.hash_token(raw),
                                   expires_at=timezone.now() + timedelta(hours=1))
    return raw


@pytest.fixture
def auth_client(raw_token):
    """A SEPARATE test client authenticated as the customer (own Client instance)."""
    from django.test import Client
    return Client(HTTP_AUTHORIZATION='Bearer ' + raw_token)


@pytest.fixture
def address(db, customer):
    from smartfood.models import Address
    return Address.objects.create(customer=customer, line='Amir Temur 12', is_default=True,
                                  lat=Decimal('41.311158'), lng=Decimal('69.279737'))


# --- helpers ------------------------------------------------------------- #
def make_init_data(bot_token, user):
    """Build a VALID Telegram initData querystring signed with bot_token."""
    import hmac
    import hashlib
    import json
    import time
    from urllib.parse import urlencode
    pairs = {
        'auth_date': str(int(time.time())),
        'query_id': 'AAHtest',
        'user': json.dumps(user, separators=(',', ':')),
    }
    dcs = '\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    pairs['hash'] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)
