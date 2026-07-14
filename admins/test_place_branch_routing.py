import pytest
from django.test import override_settings

from admins.services.place_service import PlaceService, TableService
from base.models import CashRegister, Place, Table


pytestmark = pytest.mark.django_db


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_cloud_place_and_table_inherit_the_only_operational_branch():
    CashRegister.objects.create(branch_id='branch1')

    payload, status = PlaceService.create('Main hall')
    assert status == 201
    place = Place.objects.get(pk=payload['data']['place']['id'])
    assert place.branch_id == 'branch1'

    payload, status = TableService.create(place.id, 'A1')
    assert status == 201
    table = Table.objects.get(pk=payload['data']['table']['id'])
    assert table.branch_id == 'branch1'
    assert table.place.branch_id == table.branch_id


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_cloud_place_requires_explicit_branch_when_multiple_exist():
    CashRegister.objects.create(branch_id='branch1')
    CashRegister.objects.create(branch_id='branch2')

    payload, status = PlaceService.create('Ambiguous hall')
    assert status == 422
    assert 'branch_id' in payload['errors']
    assert not Place.objects.filter(name='Ambiguous hall').exists()

    payload, status = PlaceService.create('Branch two hall', branch_id='branch2')
    assert status == 201
    assert payload['data']['place']['branch_id'] == 'branch2'


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_branch_scoped_place_lookup_does_not_cross_branches():
    place = Place.objects.create(name='Branch one hall', branch_id='branch1')

    payload, status = PlaceService.get(place.id, branch_id='branch2')

    assert status == 404
    assert payload['success'] is False


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_place_name_uniqueness_uses_the_normalized_branch_name():
    Place.objects.create(name='Main hall', branch_id='branch1')

    payload, status = PlaceService.create('  MAIN HALL  ', branch_id='branch1')

    assert status == 400
    assert payload['success'] is False
    assert Place.objects.filter(branch_id='branch1').count() == 1
