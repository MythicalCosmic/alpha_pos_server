"""Plain dict builders for the customer API.

Money is INTEGER so'm (UZS) per the frontend contract (the POS stores Decimal;
we round to whole so'm at the boundary). Catalog text is trilingual: each bot
shadow row carries uz/ru/en overrides that fall back to the POS base name.
"""
from decimal import Decimal, ROUND_HALF_UP

_LANGS = ('uz', 'ru', 'en')


def uzs(d):
    """Decimal/None -> integer so'm (UZS has no minor unit in practice)."""
    if d is None:
        d = Decimal('0')
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    return int(d.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def tri(obj, prefix, lang, fallback=''):
    """Pick obj.<prefix>_<lang>, falling back across languages then to fallback."""
    for code in (lang, 'uz', 'ru', 'en'):
        v = getattr(obj, f'{prefix}_{code}', '') or ''
        if v:
            return v
    return fallback


def names(obj, prefix, fallback=''):
    return {c: ((getattr(obj, f'{prefix}_{c}', '') or '') or fallback) for c in _LANGS}


# ---- catalog -------------------------------------------------------------- #
def category_dict(bot_cat, lang='uz'):
    c = bot_cat.category
    return {
        'id': c.id,
        'name': tri(bot_cat, 'name', lang, c.name),
        'names': names(bot_cat, 'name', c.name),
        'sort': bot_cat.sort_order,
        'image_url': bot_cat.image_url,
    }


def size_dict(s, lang='uz'):
    return {
        'id': s.id,
        'name': tri(s, 'name', lang),
        'names': names(s, 'name'),
        'price_delta': uzs(s.price_delta),
        'is_default': s.is_default,
    }


def topping_dict(t, lang='uz'):
    return {'id': t.id, 'name': tri(t, 'name', lang), 'price': uzs(t.price)}


def topping_group_dict(g, lang='uz'):
    return {
        'id': g.id,
        'name': tri(g, 'name', lang),
        'required': g.is_required,
        'min_select': g.min_select,
        'max_select': g.max_select,
        'toppings': [topping_dict(t, lang) for t in g.toppings.all() if t.is_selling],
    }


def product_dict(bot_prod, lang='uz', detail=False):
    p = bot_prod.product
    data = {
        'id': p.id,
        'category_id': p.category_id,
        'name': tri(bot_prod, 'name', lang, p.name),
        'names': names(bot_prod, 'name', p.name),
        'price': uzs(p.price),
        'image_url': bot_prod.image_url,
        'tag': bot_prod.tag,
        'kcal': bot_prod.kcal,
        'available': bot_prod.is_selling,
    }
    if detail:
        data['description'] = tri(bot_prod, 'desc', lang, p.description or '')
        data['descriptions'] = names(bot_prod, 'desc', p.description or '')
        data['sizes'] = [size_dict(s, lang) for s in p.bot_sizes.all() if s.is_selling]
        data['topping_groups'] = [topping_group_dict(g, lang) for g in p.topping_groups.all()]
    return data


# ---- customer / addresses ------------------------------------------------- #
def customer_dict(c):
    return {
        'id': c.id, 'telegram_id': c.telegram_id, 'name': c.name,
        'phone': c.phone_number, 'language': c.language, 'photo_url': c.photo_url,
        'loyalty': {'points': c.loyalty_points},
    }


def address_dict(a):
    return {
        'id': a.id, 'label': a.label, 'line': a.line,
        'lat': float(a.lat) if a.lat is not None else None,
        'lng': float(a.lng) if a.lng is not None else None,
        'city': a.city, 'street': a.street, 'house': a.house,
        'apartment': a.apartment, 'entrance': a.entrance, 'floor': a.floor,
        'intercom': a.intercom, 'comment': a.comment, 'precision': a.precision,
        'is_default': a.is_default,
    }


# ---- orders --------------------------------------------------------------- #
def bot_order_item_dict(it):
    return {
        'product_id': it.product_id, 'size_id': it.size_id, 'quantity': it.quantity,
        'unit_price': uzs(it.unit_price), 'line_total': uzs(it.line_total),
        'toppings': it.toppings_snapshot, 'detail': it.detail,
    }


def bot_order_dict(o):
    pos = o.pos_order
    return {
        'id': o.id, 'code': o.code, 'status': o.status, 'order_type': o.order_type,
        'created_at': o.created_at.isoformat() if o.created_at else None,
        'phone': o.phone_number, 'note': o.note, 'address_text': o.address_text,
        'payment_method': o.payment_method,
        'totals': {
            'subtotal': uzs(o.subtotal), 'delivery_fee': uzs(o.delivery_fee),
            'discount': uzs(o.discount), 'tip': uzs(o.tip), 'total': uzs(o.total),
        },
        'loyalty_points_used': o.loyalty_points_used,
        'loyalty_points_earned': o.loyalty_points_earned,
        'items': [bot_order_item_dict(it) for it in o.items.all()],
        'pos_order': None if not pos else {
            'id': pos.id, 'uuid': str(pos.uuid), 'status': pos.status,
            'display_id': getattr(pos, 'display_id', None),
        },
        'dispatched_at': o.dispatched_at.isoformat() if o.dispatched_at else None,
        'reject_reason': o.reject_reason,
    }


# ---- config --------------------------------------------------------------- #
def config_dict(cfg):
    return {
        'currency': cfg.currency,
        'enabled': cfg.enabled,
        'delivery_fee': uzs(cfg.delivery_fee),
        'free_delivery_threshold': uzs(cfg.free_delivery_threshold),
        'min_order_amount': uzs(cfg.min_order_amount),
        'default_tip_options': cfg.default_tip_options or [],
        'supported_languages': list(_LANGS),
        'default_language': cfg.default_lang,
        'service_area': cfg.service_area or {},
        'feature_flags': {
            'loyalty': bool(cfg.loyalty_earn_per),
            'card_payments': False,
            'scheduled_delivery': False,
        },
        'support': {
            'phone': cfg.support_phone,
            'telegram': cfg.support_telegram,
            'email': cfg.support_email,
        },
    }


# ---- loyalty: rewards, redemptions, ledger -------------------------------- #
def reward_dict(r, lang='uz', points=0):
    """A gift in the catalog. `points` is the viewer's balance (for `affordable`)."""
    return {
        'id': r.id,
        'name': tri(r, 'name', lang, 'Gift'),
        'names': names(r, 'name'),
        'description': tri(r, 'desc', lang),
        'kind': r.kind,
        'points_cost': r.points_cost,
        'image_url': r.image_url,
        'discount_amount': uzs(r.discount_amount) if r.kind == 'DISCOUNT' else None,
        'product_id': r.product_id,
        'in_stock': (r.stock is None or r.stock > 0),
        'affordable': points >= r.points_cost,
    }


def redemption_dict(r):
    return {
        'id': r.id,
        'code': r.code,
        'reward_name': r.reward_name,
        'kind': r.kind,
        'points_spent': r.points_spent,
        'status': r.status,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'fulfilled_at': r.fulfilled_at.isoformat() if r.fulfilled_at else None,
    }


def loyalty_txn_dict(t):
    """One ledger row. Keeps the legacy {code, points_earned, points_used} shape
    the Mini App history already renders, plus richer fields."""
    if t.bot_order_id:
        code = t.bot_order.code
    elif t.redemption_id:
        code = t.redemption.code
    else:
        code = t.get_kind_display()
    return {
        'kind': t.kind,
        'points': t.points,
        'balance_after': t.balance_after,
        'reason': t.reason,
        'code': code,
        'points_earned': t.points if t.points > 0 else 0,
        'points_used': -t.points if t.points < 0 else 0,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }


def member_dict(c):
    """Staff-facing customer summary (loyalty scan/lookup)."""
    return {
        'id': c.id,
        'telegram_id': c.telegram_id,
        'member_id': f'SF-{c.telegram_id}',
        'name': c.name,
        'phone': c.phone_number,
        'points': c.loyalty_points,
        'is_blocked': c.is_blocked,
    }
