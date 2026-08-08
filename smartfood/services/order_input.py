"""Strict normalization for untrusted Smart Food cart and order payloads."""

from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.conf import settings


MAX_CART_LINES = 100
MAX_TOPPINGS_PER_LINE = 50
DEFAULT_MAX_QUANTITY = 100
MAX_POINTS = 2_147_483_647
MAX_TIP = Decimal('99999999.99')
MAX_POS_TOTAL = Decimal('99999999.99')
MAX_NOTE_LENGTH = 2000
MAX_PHONE_LENGTH = 20
MAX_DATABASE_ID = 9_223_372_036_854_775_807


class OrderInputError(ValueError):
    def __init__(self, code, message, http=422, errors=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http
        self.errors = errors or {}


def _integer(value, *, field, minimum=0, maximum=None):
    if isinstance(value, bool):
        raise OrderInputError(f'invalid_{field}', f'{field} must be an integer')
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise OrderInputError(f'invalid_{field}', f'{field} must be an integer')
    if parsed < minimum or (maximum is not None and parsed > maximum):
        upper = f' and at most {maximum}' if maximum is not None else ''
        raise OrderInputError(
            f'invalid_{field}',
            f'{field} must be at least {minimum}{upper}',
        )
    return parsed


def normalize_client_order_id(value):
    if value in (None, ''):
        raise OrderInputError(
            'invalid_client_order_id',
            'client_order_id is required and must be a UUID',
            errors={'client_order_id': 'required'},
        )
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise OrderInputError(
            'invalid_client_order_id',
            'client_order_id must be a valid UUID',
            errors={'client_order_id': 'invalid UUID'},
        ) from None


def normalize_order_type(value):
    if not isinstance(value, str):
        raise OrderInputError('invalid_order_type', 'order_type must be DELIVERY or PICKUP')
    normalized = value.strip().upper()
    if normalized not in {'DELIVERY', 'PICKUP'}:
        raise OrderInputError('invalid_order_type', 'order_type must be DELIVERY or PICKUP')
    return normalized


def normalize_payment_method(value):
    if not isinstance(value, str):
        raise OrderInputError('invalid_payment_method', 'payment_method must be CASH or CARD')
    normalized = value.strip().upper()
    if normalized not in {'CASH', 'CARD'}:
        raise OrderInputError('invalid_payment_method', 'payment_method must be CASH or CARD')
    return normalized


def normalize_address_id(value, *, required):
    if value in (None, ''):
        if required:
            raise OrderInputError(
                'address_required',
                'A delivery address is required',
                errors={'address_id': 'required'},
            )
        return None
    return _integer(
        value,
        field='address_id',
        minimum=1,
        maximum=MAX_DATABASE_ID,
    )


def normalize_points(value):
    if value in (None, ''):
        return 0
    return _integer(value, field='points_used', minimum=0, maximum=MAX_POINTS)


def normalize_tip(value):
    if value in (None, ''):
        return Decimal('0.00')
    if isinstance(value, bool):
        raise OrderInputError('invalid_tip', 'tip must be a finite non-negative amount')
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise OrderInputError('invalid_tip', 'tip must be a finite non-negative amount') from None
    if not parsed.is_finite() or parsed < 0 or parsed > MAX_TIP:
        raise OrderInputError('invalid_tip', 'tip must be a finite non-negative amount')
    if parsed.as_tuple().exponent < -2:
        raise OrderInputError('invalid_tip', 'tip may have at most two decimal places')
    return parsed.quantize(Decimal('0.01'))


def normalize_phone(value, fallback=''):
    if value in (None, ''):
        value = fallback or ''
    if not isinstance(value, str):
        raise OrderInputError('invalid_phone', 'phone must be a string')
    value = value.strip()
    if len(value) > MAX_PHONE_LENGTH:
        raise OrderInputError(
            'invalid_phone',
            f'phone must be at most {MAX_PHONE_LENGTH} characters',
        )
    return value


def normalize_note(value):
    if value in (None, ''):
        return ''
    if not isinstance(value, str):
        raise OrderInputError('invalid_note', 'note must be a string')
    value = value.strip()
    if len(value) > MAX_NOTE_LENGTH:
        raise OrderInputError(
            'invalid_note',
            f'note must be at most {MAX_NOTE_LENGTH} characters',
        )
    return value


def normalize_cart_items(items):
    if not isinstance(items, list):
        raise OrderInputError('invalid_items', 'items must be a non-empty array')
    if not items:
        raise OrderInputError('empty_cart', 'Cart is empty')
    if len(items) > MAX_CART_LINES:
        raise OrderInputError('too_many_items', f'Cart may contain at most {MAX_CART_LINES} lines')

    max_quantity = max(
        1,
        int(getattr(settings, 'SMARTFOOD_MAX_ITEM_QUANTITY', DEFAULT_MAX_QUANTITY)),
    )
    normalized = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise OrderInputError(
                'invalid_items',
                f'items[{index}] must be an object',
            )
        product_id = _integer(
            raw.get('product_id'),
            field='product_id',
            minimum=1,
            maximum=MAX_DATABASE_ID,
        )
        quantity = _integer(
            raw.get('quantity', 1),
            field='quantity',
            minimum=1,
            maximum=max_quantity,
        )
        size_raw = raw.get('size_id')
        size_id = None if size_raw in (None, '') else _integer(
            size_raw,
            field='size_id',
            minimum=1,
            maximum=MAX_DATABASE_ID,
        )
        topping_raw = raw.get('topping_ids', [])
        if topping_raw in (None, ''):
            topping_raw = []
        if not isinstance(topping_raw, list):
            raise OrderInputError('invalid_topping_ids', 'topping_ids must be an array')
        if len(topping_raw) > MAX_TOPPINGS_PER_LINE:
            raise OrderInputError(
                'invalid_topping_ids',
                f'A line may contain at most {MAX_TOPPINGS_PER_LINE} toppings',
            )
        topping_ids = [
            _integer(
                topping_id,
                field='topping_id',
                minimum=1,
                maximum=MAX_DATABASE_ID,
            )
            for topping_id in topping_raw
        ]
        if len(set(topping_ids)) != len(topping_ids):
            raise OrderInputError('invalid_topping_ids', 'Duplicate topping_ids are not allowed')
        normalized.append({
            'product_id': product_id,
            'quantity': quantity,
            'size_id': size_id,
            'topping_ids': sorted(topping_ids),
        })
    return normalized


def error_response(error):
    body = {
        'success': False,
        'code': error.code,
        'message': error.message,
    }
    if error.errors:
        body['errors'] = error.errors
    return body, error.http
