"""Cart pricing + validation — the money authority.

NEVER trusts client prices: every line's unit_price is recomputed from the LIVE
POS product price + size delta + chosen topping prices, and each line is
re-validated against publish / stop-selling at the moment of quote/submit (so an
item that sells out between browsing and checkout is rejected). Required/min/max
topping-group rules are enforced too.
"""
from decimal import Decimal

from base.helpers.response import ServiceResponse
from smartfood.models import BotConfig, BotProduct, Size, Topping, ToppingGroup
from smartfood.serializers import tri, uzs

CENT = Decimal('0.01')


class CartError(Exception):
    """Raised when a cart can't be priced (unavailable item, bad qty, group rules)."""
    def __init__(self, code, message, http=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


def _priced_line(item, lang):
    """item = {product_id, size_id?, topping_ids?[], quantity}. Returns a frozen
    priced line or raises CartError."""
    product_id = item.get('product_id')
    try:
        quantity = int(item.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        raise CartError('invalid_quantity', 'Quantity must be greater than 0', 422)

    # Mirror the browse filter EXACTLY: a published+selling product whose CATEGORY
    # is unpublished/stopped must be rejected at submit too (else a sold-out
    # category still takes orders).
    bp = (BotProduct.objects.select_related('product', 'product__category__bot')
          .filter(product_id=product_id, is_published=True,
                  product__category__bot__is_published=True,
                  product__category__bot__is_selling=True).first())
    if not bp or bp.product.is_deleted:
        raise CartError('item_unavailable', 'Product is not available', 409)
    if not bp.is_selling:
        raise CartError('item_unavailable', f'{bp.product.name} is sold out', 409)
    product = bp.product
    unit = Decimal(product.price)

    size = None
    size_id = item.get('size_id')
    if size_id:
        size = Size.objects.filter(id=size_id, product_id=product.id).first()
        if not size:
            raise CartError('item_unavailable', 'Selected size is invalid', 409)
        if not size.is_selling:
            raise CartError('item_unavailable', 'Selected size is unavailable', 409)
        unit += Decimal(size.price_delta)

    topping_ids = item.get('topping_ids') or []
    toppings_snapshot = []
    chosen_by_group = {}
    if topping_ids:
        toppings = list(Topping.objects.select_related('group')
                        .filter(id__in=topping_ids, group__product_id=product.id))
        found = {t.id for t in toppings}
        if any(tid not in found for tid in topping_ids):
            raise CartError('item_unavailable', 'A selected topping is invalid', 409)
        for t in toppings:
            if not t.is_selling:
                raise CartError('item_unavailable', 'A selected topping is sold out', 409)
            unit += Decimal(t.price)
            toppings_snapshot.append({'topping_id': t.id, 'name': tri(t, 'name', lang),
                                      'price': uzs(t.price)})
            chosen_by_group[t.group_id] = chosen_by_group.get(t.group_id, 0) + 1

    # Required / min / max per option group.
    #  - required  -> must choose at least max(min_select, 1)
    #  - optional  -> min_select only applies if the customer picked any (>0)
    for g in ToppingGroup.objects.filter(product_id=product.id):
        n = chosen_by_group.get(g.id, 0)
        if g.is_required:
            need = max(g.min_select, 1)
            if n < need:
                raise CartError('topping_required',
                                f'Choose at least {need} for {tri(g, "name", lang)}', 422)
        elif n and g.min_select and n < g.min_select:
            raise CartError('topping_min',
                            f'Choose at least {g.min_select} for {tri(g, "name", lang)}', 422)
        if g.max_select and n > g.max_select:
            raise CartError('topping_max',
                            f'Choose at most {g.max_select} for {tri(g, "name", lang)}', 422)

    unit = unit.quantize(CENT)
    line_total = (unit * quantity).quantize(CENT)
    bits = ([tri(size, 'name', lang)] if size else []) + [t['name'] for t in toppings_snapshot]
    detail = ' · '.join(b for b in bits if b)
    return {
        'product': product, 'bot_product': bp, 'size': size,
        'product_id': product.id, 'size_id': size.id if size else None,
        'quantity': quantity, 'unit_price': unit, 'line_total': line_total,
        'toppings_snapshot': toppings_snapshot, 'detail': detail,
        'name': tri(bp, 'name', lang, product.name),
    }


def price_cart(items, order_type='DELIVERY', tip=0, points_used=0, customer=None, lang='uz'):
    """Price a whole cart. Returns a dict of priced lines + totals; raises CartError."""
    if not items:
        raise CartError('empty_cart', 'Cart is empty', 422)
    cfg = BotConfig.load()
    lines = [_priced_line(it, lang) for it in items]
    subtotal = sum((ln['line_total'] for ln in lines), Decimal('0.00')).quantize(CENT)

    if cfg.min_order_amount and subtotal < Decimal(cfg.min_order_amount):
        raise CartError('min_order', f'Minimum order is {uzs(cfg.min_order_amount)}', 422)

    if order_type == 'PICKUP':
        delivery_fee, free = Decimal('0.00'), True
    elif cfg.free_delivery_threshold and subtotal >= Decimal(cfg.free_delivery_threshold):
        delivery_fee, free = Decimal('0.00'), True
    else:
        delivery_fee, free = Decimal(cfg.delivery_fee), False

    points_used = max(0, int(points_used or 0))
    discount = Decimal('0.00')
    if points_used and cfg.loyalty_point_value and customer is not None:
        points_used = min(points_used, max(0, customer.loyalty_points))
        discount = min(Decimal(points_used) * Decimal(cfg.loyalty_point_value), subtotal)
    else:
        points_used = 0

    try:
        tip_d = Decimal(str(tip or 0))
    except Exception:
        tip_d = Decimal('0.00')
    if tip_d < 0:
        tip_d = Decimal('0.00')

    total = (subtotal + delivery_fee + tip_d - discount).quantize(CENT)
    if total < 0:
        total = Decimal('0.00')

    earned = 0
    if cfg.loyalty_earn_per and Decimal(cfg.loyalty_earn_per) > 0:
        earned = int(subtotal / Decimal(cfg.loyalty_earn_per))

    return {
        'lines': lines,
        'subtotal': subtotal,
        'delivery_fee': delivery_fee.quantize(CENT),
        'free_delivery_applied': free,
        'discount': discount.quantize(CENT),
        'tip': tip_d.quantize(CENT),
        'total': total,
        'points_used': points_used,
        'points_earned': earned,
    }


def quote_dict(priced, lang='uz'):
    """FE-facing quote payload (integer UZS)."""
    return {
        'currency': BotConfig.load().currency,
        'subtotal': uzs(priced['subtotal']),
        'delivery_fee': uzs(priced['delivery_fee']),
        'free_delivery_applied': priced['free_delivery_applied'],
        'discount': uzs(priced['discount']),
        'tip': uzs(priced['tip']),
        'total': uzs(priced['total']),
        'loyalty_points_used': priced['points_used'],
        'loyalty_points_earned': priced['points_earned'],
        'lines': [{
            'product_id': ln['product_id'], 'size_id': ln['size_id'], 'name': ln['name'],
            'quantity': ln['quantity'], 'unit_price': uzs(ln['unit_price']),
            'line_total': uzs(ln['line_total']), 'toppings': ln['toppings_snapshot'],
            'detail': ln['detail'],
        } for ln in priced['lines']],
    }


class CartService:
    @staticmethod
    def quote(items, order_type='DELIVERY', tip=0, points_used=0, customer=None, lang='uz'):
        try:
            priced = price_cart(items, order_type, tip, points_used, customer, lang)
        except CartError as e:
            return {'success': False, 'code': e.code, 'message': e.message}, e.http
        return ServiceResponse.success(data=quote_dict(priced, lang))
