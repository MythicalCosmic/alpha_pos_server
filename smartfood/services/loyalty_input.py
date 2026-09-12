"""Strict limits for loyalty adjustments and operator input."""
import re

from smartfood.services.order_input import OrderInputError

MAX_POINTS = 2_147_483_647


def whole_number(value, field, *, minimum=-MAX_POINTS, maximum=MAX_POINTS):
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r'-?[0-9]{1,19}', value.strip()):
        parsed = int(value.strip())
    else:
        raise OrderInputError('INVALID_LOYALTY_INPUT', f'{field} must be a whole number.',
                              errors={field: 'Use an unformatted whole number.'})
    if not minimum <= parsed <= maximum:
        raise OrderInputError('INVALID_LOYALTY_INPUT', f'{field} exceeds the supported limit.',
                              errors={field: f'Use a value from {minimum} to {maximum}.'})
    return parsed


def reason_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise OrderInputError('REASON_REQUIRED', 'A reason of 1–200 characters is required.',
                              errors={'reason': 'Enter a reason of 1–200 characters.'})
    return value.strip()


def input_error(error):
    return {'success': False, 'code': error.code, 'message': error.message,
            'errors': error.errors}, error.http


def loyalty_errors(view):
    """Translate domain failures after the aggregate transaction rolls back."""
    from functools import wraps
    from django.http import JsonResponse

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except OrderInputError as error:
            body, status = input_error(error)
            return JsonResponse(body, status=status)
    return wrapped
