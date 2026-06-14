"""Customer delivery addresses + a thin Yandex geocoder proxy.

Addresses are owned by the Customer; exactly one may be is_default (flipping one
on flips the rest off, in a transaction). Geocoding is best-effort: with no
YANDEX_GEOCODER_KEY configured the proxy returns a clean 400 rather than failing
hard. Yandex wants geocode="<lng>,<lat>" (longitude first) but our JSON keeps
the {lat,lng} order the Mini App speaks.
"""
import logging

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


def _apply(address, fields):
    for key in _FIELDS:
        if key in fields and fields[key] is not None:
            setattr(address, key, fields[key])


class AddressService:
    @staticmethod
    def list_for(customer):
        rows = customer.addresses.all()
        return ServiceResponse.success(data={'items': [address_dict(a) for a in rows]})

    @staticmethod
    @transaction.atomic
    def create(customer, **fields):
        line = (fields.get('line') or '').strip() if fields.get('line') is not None else ''
        if not line:
            return ServiceResponse.validation_error({'line': 'required'},
                                                    'An address line is required')
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
        if 'line' in fields and fields['line'] is not None and not str(fields['line']).strip():
            return ServiceResponse.validation_error({'line': 'required'},
                                                    'An address line is required')
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
