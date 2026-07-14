from django.conf import settings

from base.helpers.response import ServiceResponse
from base.models import CashRegister
from base.repositories import PlaceRepository, TableRepository


def _resolve_branch_id(branch_id=None):
    """Resolve the operational branch targeted by an admin place mutation.

    A local node is already bound to one branch.  The cloud may infer a target
    only when exactly one operational register exists; a multi-branch install
    must send ``branch_id`` explicitly instead of creating invisible
    ``branch_id='cloud'`` rows.
    """
    requested = str(branch_id or '').strip()
    node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
    if requested:
        return requested, None
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
        if node_branch:
            return node_branch, None
    else:
        candidates = list(
            CashRegister.objects.filter(is_deleted=False)
            .exclude(branch_id__in=['', node_branch])
            .order_by()
            .values_list('branch_id', flat=True)
            .distinct()[:2]
        )
        if len(candidates) == 1:
            return candidates[0], None

    return None, ServiceResponse.validation_error(
        errors={'branch_id': 'Required unless exactly one operational branch exists'},
        message='Choose a branch',
    )


def _get_place(place_id, branch_id):
    return PlaceRepository.filter(id=place_id, branch_id=branch_id).first()


def _get_table(table_id, branch_id):
    return TableRepository.filter(id=table_id, branch_id=branch_id).first()


def _serialize_place(place):
    return {
        'id': place.id,
        'uuid': str(place.uuid),
        'branch_id': place.branch_id,
        'name': place.name,
        'place_type': place.place_type,
        'capacity': place.capacity,
        'is_active': place.is_active,
        'sort_order': place.sort_order,
        'created_at': place.created_at.isoformat(),
        'updated_at': place.updated_at.isoformat(),
    }


def _serialize_table(table):
    return {
        'id': table.id,
        'uuid': str(table.uuid),
        'branch_id': table.branch_id,
        'place': {
            'id': table.place.id,
            'name': table.place.name,
        },
        'number': table.number,
        'capacity': table.capacity,
        'status': table.status,
        'is_active': table.is_active,
        'sort_order': table.sort_order,
    }


VALID_PLACE_TYPES = ['HALL', 'BAR', 'TERRACE', 'PRIVATE_ROOM', 'OUTDOOR']
VALID_TABLE_STATUSES = ['AVAILABLE', 'OCCUPIED', 'RESERVED', 'OUT_OF_SERVICE']


class PlaceService:

    @staticmethod
    def list(
        page=1, per_page=20, place_type=None, is_active=None, branch_id=None,
    ):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        qs = PlaceRepository.get_all().filter(branch_id=branch_id)

        if place_type:
            qs = qs.filter(place_type=place_type)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        qs = qs.order_by('sort_order', 'name')
        page_obj, paginator = PlaceRepository.paginate(qs, page, per_page)
        places = [_serialize_place(p) for p in page_obj.object_list]

        return ServiceResponse.success(data={
            'places': places,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_places': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get(place_id, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        place = _get_place(place_id, branch_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        data = _serialize_place(place)
        data['table_count'] = place.tables.filter(is_deleted=False).count()

        return ServiceResponse.success(data={'place': data})

    @staticmethod
    def create(name, place_type='HALL', capacity=0, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        if not name or not name.strip():
            return ServiceResponse.validation_error(
                errors={'name': 'Name is required'},
                message='Validation failed',
            )
        clean_name = name.strip()

        if place_type not in VALID_PLACE_TYPES:
            return ServiceResponse.validation_error(
                errors={'place_type': f'Must be one of: {", ".join(VALID_PLACE_TYPES)}'},
                message='Invalid place type',
            )

        if PlaceRepository.filter(
            branch_id=branch_id, name__iexact=clean_name,
        ).exists():
            return ServiceResponse.error('A place with this name already exists')

        place = PlaceRepository.create(
            name=clean_name,
            place_type=place_type,
            capacity=capacity,
            branch_id=branch_id,
        )

        return ServiceResponse.created(
            data={'place': _serialize_place(place)},
            message='Place created successfully',
        )

    @staticmethod
    def update(place_id, branch_id=None, **kwargs):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        place = _get_place(place_id, branch_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        if 'name' in kwargs:
            name = kwargs['name']
            if not name or not name.strip():
                return ServiceResponse.validation_error(
                    errors={'name': 'Name is required'},
                    message='Validation failed',
                )
            clean_name = name.strip()
            if PlaceRepository.filter(
                branch_id=branch_id, name__iexact=clean_name,
            ).exclude(id=place_id).exists():
                return ServiceResponse.error('A place with this name already exists')
            kwargs['name'] = clean_name

        if 'place_type' in kwargs and kwargs['place_type'] not in VALID_PLACE_TYPES:
            return ServiceResponse.validation_error(
                errors={'place_type': f'Must be one of: {", ".join(VALID_PLACE_TYPES)}'},
                message='Invalid place type',
            )

        place = PlaceRepository.update(place, **kwargs)

        return ServiceResponse.success(
            data={'place': _serialize_place(place)},
            message='Place updated successfully',
        )

    @staticmethod
    def delete(place_id, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        place = _get_place(place_id, branch_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        active_tables = place.tables.filter(is_deleted=False, is_active=True).count()
        if active_tables > 0:
            return ServiceResponse.error(
                f'Cannot delete place with {active_tables} active table(s). Deactivate or delete tables first.'
            )

        PlaceRepository.delete(place)

        return ServiceResponse.success(message='Place deleted successfully')


class TableService:

    @staticmethod
    def list(page=1, per_page=20, place_id=None, status=None, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        qs = (
            TableRepository.get_all()
            .filter(branch_id=branch_id)
            .select_related('place')
        )

        if place_id:
            qs = qs.filter(place_id=place_id)
        if status:
            qs = qs.filter(status=status)

        qs = qs.order_by('place', 'sort_order', 'number')
        page_obj, paginator = TableRepository.paginate(qs, page, per_page)
        tables = [_serialize_table(t) for t in page_obj.object_list]

        return ServiceResponse.success(data={
            'tables': tables,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_tables': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get(table_id, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        table = _get_table(table_id, branch_id)
        if not table:
            return ServiceResponse.not_found('Table not found')

        return ServiceResponse.success(data={'table': _serialize_table(table)})

    @staticmethod
    def create(place_id, number, capacity=4, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        place = _get_place(place_id, branch_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        if not number or not str(number).strip():
            return ServiceResponse.validation_error(
                errors={'number': 'Table number is required'},
                message='Validation failed',
            )

        if TableRepository.number_exists(place_id, number):
            return ServiceResponse.error(
                f'Table number {number} already exists in {place.name}'
            )

        table = TableRepository.create(
            place=place,
            number=str(number).strip(),
            capacity=capacity,
            branch_id=place.branch_id,
        )

        return ServiceResponse.created(
            data={'table': _serialize_table(table)},
            message='Table created successfully',
        )

    @staticmethod
    def update(table_id, branch_id=None, **kwargs):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        table = _get_table(table_id, branch_id)
        if not table:
            return ServiceResponse.not_found('Table not found')

        if 'number' in kwargs:
            number = kwargs['number']
            if not number or not str(number).strip():
                return ServiceResponse.validation_error(
                    errors={'number': 'Table number is required'},
                    message='Validation failed',
                )
            place_id = kwargs.get('place_id', table.place_id)
            if TableRepository.number_exists(place_id, number, exclude_id=table_id):
                return ServiceResponse.error(
                    f'Table number {number} already exists in this place'
                )
            kwargs['number'] = str(number).strip()

        if 'status' in kwargs and kwargs['status'] not in VALID_TABLE_STATUSES:
            return ServiceResponse.validation_error(
                errors={'status': f'Must be one of: {", ".join(VALID_TABLE_STATUSES)}'},
                message='Invalid table status',
            )

        if 'place_id' in kwargs:
            place = _get_place(kwargs['place_id'], branch_id)
            if not place:
                return ServiceResponse.not_found('Place not found')
            kwargs['place'] = place
            kwargs['branch_id'] = place.branch_id
            del kwargs['place_id']

        table = TableRepository.update(table, **kwargs)

        return ServiceResponse.success(
            data={'table': _serialize_table(table)},
            message='Table updated successfully',
        )

    @staticmethod
    def delete(table_id, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        table = _get_table(table_id, branch_id)
        if not table:
            return ServiceResponse.not_found('Table not found')

        TableRepository.delete(table)

        return ServiceResponse.success(message='Table deleted successfully')

    @staticmethod
    def update_status(table_id, status, branch_id=None):
        if status not in VALID_TABLE_STATUSES:
            return ServiceResponse.validation_error(
                errors={'status': f'Must be one of: {", ".join(VALID_TABLE_STATUSES)}'},
                message='Invalid table status',
            )

        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        table = _get_table(table_id, branch_id)
        if not table:
            return ServiceResponse.not_found('Table not found')
        table.status = status
        table.save(update_fields=['status'])

        return ServiceResponse.success(
            data={'table': _serialize_table(table)},
            message=f'Table status updated to {status}',
        )

    @staticmethod
    def get_for_place(place_id, branch_id=None):
        branch_id, error = _resolve_branch_id(branch_id)
        if error:
            return error
        place = _get_place(place_id, branch_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        tables = TableRepository.get_for_place(place_id).select_related('place')
        return ServiceResponse.success(data={
            'place': {'id': place.id, 'name': place.name},
            'tables': [_serialize_table(t) for t in tables],
        })
