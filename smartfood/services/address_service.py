"""Customer delivery addresses + a thin Yandex geocoder proxy.

Addresses are owned by the Customer; exactly one may be is_default (flipping one
on flips the rest off, in a transaction). Geocoding is best-effort: with no
YANDEX_GEOCODER_KEY configured the proxy returns a clean 400 rather than failing
hard. Yandex wants geocode="<lng>,<lat>" (longitude first) but our JSON keeps
the {lat,lng} order the Mini App speaks.
"""
import logging
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction

from base.helpers.response import ServiceResponse
from smartfood.models import Address
from smartfood.serializers import address_dict

logger = logging.getLogger(__name__)

_YANDEX_URL = 'https://geocode-maps.yandex.ru/1.x/'

# Fields a client may set on an address (make_default handled separately).
_FIELDS = (
    'label', 'line', 'lat', 'lng', 'city', 'street', 'house', 'apartment',
    'entrance', 'floor', 'intercom', 'comment', 'precision',
)
_TEXT_LIMITS = {
    'label': 40,
    'line': 500,
    'city': 80,
    'street': 120,
    'house': 40,
    'apartment': 40,
    'entrance': 40,
    'floor': 40,
    'intercom': 40,
    'comment': 500,
    'precision': 16,
}


def _apply(address, fields):
    for key in _FIELDS:
        if key in fields and fields[key] is not None:
            setattr(address, key, fields[key])


def _clean_text(fields):
    cleaned = {}
    errors = {}
    for key, limit in _TEXT_LIMITS.items():
        if key not in fields or fields[key] is None:
            continue
        if not isinstance(fields[key], str):
            errors[key] = 'Must be text.'
            continue
        value = fields[key].strip()
        if len(value) > limit:
            errors[key] = f'Must be {limit} characters or fewer.'
            continue
        cleaned[key] = value
    return cleaned, errors


def _coordinates(fields, address=None):
    """Return validated coordinates for the resulting address state."""
    raw_lat = fields.get('lat', getattr(address, 'lat', None))
    raw_lng = fields.get('lng', getattr(address, 'lng', None))
    errors = {}
    parsed = {}
    for key, raw, minimum, maximum in (
        ('lat', raw_lat, Decimal('-90'), Decimal('90')),
        ('lng', raw_lng, Decimal('-180'), Decimal('180')),
    ):
        if raw in (None, '') or isinstance(raw, bool):
            errors[key] = 'Choose this address on the map.'
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            errors[key] = 'Enter a valid coordinate.'
            continue
        if not value.is_finite() or value < minimum or value > maximum:
            errors[key] = 'Coordinate is outside the valid range.'
            continue
        parsed[key] = value
    return parsed, errors


class AddressService:
    @staticmethod
    def list_for(customer):
        rows = customer.addresses.all()
        return ServiceResponse.success(data={'items': [address_dict(a) for a in rows]})

    @staticmethod
    @transaction.atomic
    def create(customer, **fields):
        cleaned, errors = _clean_text(fields)
        line = cleaned.get('line', '')
        if not line:
            errors['line'] = 'An address line is required.'
        coordinates, coordinate_errors = _coordinates(fields)
        errors.update(coordinate_errors)
        if errors:
            return ServiceResponse.validation_error(
                errors,
                'Correct the delivery address.',
            )
        fields.update(cleaned)
        fields.update(coordinates)
        is_first = not customer.addresses.exists()
        make_default = bool(fields.get('make_default')) or is_first

        address = Address(customer=customer)
        _apply(address, fields)
        address.line = line
        address.is_default = make_default
        address.save()

        if make_default:
            Address.objects.filter(customer=customer).exclude(id=address.id).update(is_default=False)
        return ServiceResponse.created(data=address_dict(address))

    @staticmethod
    @transaction.atomic
    def update(customer, address_id, **fields):
        address = Address.objects.filter(id=address_id, customer=customer).first()
        if not address:
            return ServiceResponse.not_found('Address not found')
        cleaned, errors = _clean_text(fields)
        if 'line' in fields and not cleaned.get('line'):
            errors['line'] = 'An address line is required.'
        coordinates, coordinate_errors = _coordinates(fields, address)
        errors.update(coordinate_errors)
        if errors:
            return ServiceResponse.validation_error(
                errors,
                'Correct the delivery address.',
            )
        fields.update(cleaned)
        fields.update(coordinates)
        _apply(address, fields)
        make_default = bool(fields.get('make_default'))
        if make_default:
            address.is_default = True
        address.save()
        if make_default:
            Address.objects.filter(customer=customer).exclude(id=address.id).update(is_default=False)
        return ServiceResponse.success(data=address_dict(address))

    @staticmethod
    @transaction.atomic
    def delete(customer, address_id):
        address = Address.objects.filter(id=address_id, customer=customer).first()
        if not address:
            return ServiceResponse.not_found('Address not found')
        was_default = address.is_default
        address.delete()
        if was_default:
            # Promote the most recent remaining address to default.
            nxt = Address.objects.filter(customer=customer).order_by('-id').first()
            if nxt and not nxt.is_default:
                nxt.is_default = True
                nxt.save(update_fields=['is_default', 'updated_at'])
        return ServiceResponse.success(message='Address deleted')

    @staticmethod
    @transaction.atomic
    def set_default(customer, address_id):
        address = Address.objects.filter(id=address_id, customer=customer).first()
        if not address:
            return ServiceResponse.not_found('Address not found')
        if not address.is_default:
            address.is_default = True
            address.save(update_fields=['is_default', 'updated_at'])
        Address.objects.filter(customer=customer).exclude(id=address.id).update(is_default=False)
        return ServiceResponse.success(message='Default address set')

    # ---- Yandex geocoder proxy (best-effort) ------------------------------- #
    @staticmethod
    def _geocode(geocode, lang, limit):
        key = getattr(settings, 'YANDEX_GEOCODER_KEY', '')
        if not key:
            return ServiceResponse.error('Geocoding not configured')
        params = {
            'apikey': key,
            'geocode': geocode,
            'format': 'json',
            'lang': lang or 'ru',
            'results': limit,
        }
        try:
            resp = requests.get(_YANDEX_URL, params=params, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            logger.debug('yandex geocode failed', exc_info=True)
            return ServiceResponse.error('Geocoding service unavailable')
        return ServiceResponse.success(data={'results': _parse_geo(payload)})

    @staticmethod
    def geocode_reverse(lat, lng, lang='ru'):
        if lat is None or lng is None:
            return ServiceResponse.validation_error({'lat': 'required', 'lng': 'required'},
                                                    'lat and lng are required')
        # Yandex expects "<lng>,<lat>" (longitude first).
        return AddressService._geocode(f'{lng},{lat}', lang, 1)

    @staticmethod
    def geocode_forward(q, lang='ru', limit=5):
        if not q or not str(q).strip():
            return ServiceResponse.validation_error({'q': 'required'}, 'A search query is required')
        try:
            limit = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            limit = 5
        return AddressService._geocode(str(q).strip(), lang, limit)


def _parse_geo(payload):
    """GeoObjectCollection -> [{formatted, lat, lng, precision, kind}]."""
    results = []
    try:
        members = (payload['response']['GeoObjectCollection']['featureMember'])
    except (KeyError, TypeError):
        return results
    for member in members:
        geo = member.get('GeoObject') or {}
        meta = (geo.get('metaDataProperty') or {}).get('GeocoderMetaData') or {}
        pos = geo.get('Point', {}).get('pos', '')   # "<lng> <lat>"
        lng = lat = None
        if pos:
            parts = pos.split()
            if len(parts) == 2:
                try:
                    lng, lat = float(parts[0]), float(parts[1])
                except (TypeError, ValueError):
                    lng = lat = None
        results.append({
            'formatted': meta.get('text') or geo.get('name') or '',
            'lat': lat,
            'lng': lng,
            'precision': meta.get('precision', ''),
            'kind': meta.get('kind', ''),
        })
    return results
