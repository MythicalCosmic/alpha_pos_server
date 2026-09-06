"""Reject malformed order input before it reaches money or stock writes."""
import json
import pytest
from django.test import RequestFactory
from admins.requests.order_requests import create_order_request


def _parse(items):
    request = RequestFactory().post('/orders', data=json.dumps({'user_id': 1, 'items': items}), content_type='application/json')
    return create_order_request(request)


@pytest.mark.parametrize('item', [None, 5, True, 'product_id', ['product_id']])
def test_create_rejects_non_object_items(item):
    data, error = _parse([item])
    assert data is None
    assert error[1] == 422


@pytest.mark.parametrize('product_id', [True, 1.5, {'id': 1}, [1], 'abc', 2**63])
def test_create_rejects_invalid_product_identifiers(product_id):
    data, error = _parse([{'product_id': product_id, 'quantity': 1}])
    assert data is None
    assert error[1] == 422


@pytest.mark.parametrize('quantity', [True, 2**31])
def test_create_rejects_boolean_or_unstorable_quantity(quantity):
    data, error = _parse([{'product_id': 1, 'quantity': quantity}])
    assert data is None
    assert error[1] == 422


def test_create_normalizes_numeric_product_identifier_for_service_lookup():
    data, error = _parse([{'product_id': '1', 'quantity': 1}])
    assert error is None
    assert data['items'][0]['product_id'] == 1


@pytest.mark.parametrize('product_id', [True, 1.5, {'id': 1}, [1], 'abc', 2**63])
@pytest.mark.parametrize('package, service_name, method_name', [('admins', 'AdminOrderService', 'add_item_to_order')])
def test_add_item_rejects_invalid_id_before_business_writes(product_id, package, service_name, method_name, monkeypatch):
    import importlib
    import inspect
    from types import SimpleNamespace
    from unittest.mock import Mock

    views = importlib.import_module(f'{package}.views.order_views')
    service = getattr(views, service_name)
    operation = Mock(side_effect=AssertionError('invalid input reached business writes'))
    monkeypatch.setattr(service, method_name, operation)
    request = RequestFactory().post('/orders/1/add-item', data=json.dumps({'product_id': product_id}), content_type='application/json')
    request.user = SimpleNamespace(id=1, role='ADMIN')
    response = inspect.unwrap(views.add_item)(request, 1)
    assert response.status_code == 422
    operation.assert_not_called()


@pytest.mark.parametrize('package, service_name, method_name', [('admins', 'AdminOrderService', 'add_item_to_order')])
def test_add_item_normalizes_numeric_string_id(package, service_name, method_name, monkeypatch):
    import importlib
    import inspect
    from types import SimpleNamespace
    from unittest.mock import Mock

    views = importlib.import_module(f'{package}.views.order_views')
    operation = Mock(return_value=({'success': True}, 200))
    monkeypatch.setattr(getattr(views, service_name), method_name, operation)
    request = RequestFactory().post('/orders/1/add-item', data=json.dumps({'product_id': '001', 'quantity': 2}), content_type='application/json')
    request.user = SimpleNamespace(id=1, role='ADMIN')
    assert inspect.unwrap(views.add_item)(request, 1).status_code == 200
    assert operation.call_args.args[:3] == (1, 1, 2)
