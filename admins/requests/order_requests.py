import json

from base.helpers.request import coerce_positive_id, coerce_quantity


def create_order_request(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None, ({"success": False, "message": "Invalid JSON"}, 400)

    if not isinstance(data, dict):
        return None, ({"success": False, "message": "Expected JSON object"}, 400)

    if 'user_id' not in data or data['user_id'] is None or data['user_id'] == '':
        return None, ({
            "success": False,
            "message": "Missing required fields: user_id",
            "errors": {"user_id": "user_id is required"},
        }, 422)

    for field in ('user_id', 'cashier_id', 'delivery_person_id'):
        if field not in data:
            continue
        if data[field] is None or data[field] == '':
            data[field] = None
            continue
        value = coerce_positive_id(data[field])
        if value is None:
            return None, ({
                'success': False, 'message': f'Invalid {field}',
                'errors': {field: 'Must be a positive integer ID'},
            }, 422)
        data[field] = value

    items = data.get('items')
    if not items or not isinstance(items, list) or len(items) == 0:
        return None, ({
            "success": False,
            "message": "Order must contain items",
            "errors": {"items": "At least one item is required"},
        }, 422)

    order_type = data.get('order_type', 'HALL')
    if order_type not in ['HALL', 'DELIVERY', 'PICKUP']:
        return None, ({
            "success": False,
            "message": "Invalid order type",
            "errors": {"order_type": "Must be HALL, DELIVERY, or PICKUP"},
        }, 422)

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return None, ({
                "success": False,
                "message": f"Invalid item {idx}",
                "errors": {f"items[{idx}]": "Each item must be an object"},
            }, 422)
        if 'product_id' not in item:
            return None, ({
                "success": False,
                "message": f"Item {idx} missing product_id",
                "errors": {f"items[{idx}].product_id": "product_id is required"},
            }, 422)
        product_id = coerce_positive_id(item['product_id'])
        if product_id is None:
            return None, ({
                "success": False,
                "message": f"Invalid product_id for item {idx}",
                "errors": {f"items[{idx}].product_id": "Must be a positive integer ID"},
            }, 422)
        item['product_id'] = product_id
        qty = item.get('quantity', 1)
        if not isinstance(qty, int) or coerce_quantity(qty) is None:
            return None, ({
                "success": False,
                "message": f"Invalid quantity for item {idx}",
                "errors": {f"items[{idx}].quantity": "quantity must be an integer from 1 to 2147483647"},
            }, 422)

    return data, None


def update_order_request(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None, ({"success": False, "message": "Invalid JSON"}, 400)

    if not isinstance(data, dict):
        return None, ({"success": False, "message": "Expected JSON object"}, 400)

    return data, None


def bulk_ids_request(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None, ({"success": False, "message": "Invalid JSON"}, 400)

    if not isinstance(data, dict):
        return None, ({"success": False, "message": "Expected JSON object"}, 400)

    if not data.get('ids') or not isinstance(data['ids'], list):
        return None, ({
            "success": False,
            "message": "Missing required fields: ids",
            "errors": {"ids": "ids is required"},
        }, 422)

    return data, None
