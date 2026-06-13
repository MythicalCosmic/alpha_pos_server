from base.helpers.response import ServiceResponse
from base.repositories import PlaceRepository, TableRepository


def _serialize_place(place):
    return {
        'id': place.id,
        'uuid': str(place.uuid),
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
    def list(page=1, per_page=20, place_type=None, is_active=None):
        qs = PlaceRepository.get_all()

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
    def get(place_id):
        place = PlaceRepository.get_by_id(place_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        data = _serialize_place(place)
        data['table_count'] = place.tables.filter(is_deleted=False).count()

        return ServiceResponse.success(data={'place': data})

    @staticmethod
    def create(name, place_type='HALL', capacity=0):
        if not name or not name.strip():
            return ServiceResponse.validation_error(
                errors={'name': 'Name is required'},
                message='Validation failed',
            )

        if place_type not in VALID_PLACE_TYPES:
            return ServiceResponse.validation_error(
                errors={'place_type': f'Must be one of: {", ".join(VALID_PLACE_TYPES)}'},
                message='Invalid place type',
            )

        if PlaceRepository.name_exists(name):
            return ServiceResponse.error('A place with this name already exists')

        place = PlaceRepository.create(
            name=name.strip(),
            place_type=place_type,
            capacity=capacity,
        )

        return ServiceResponse.created(
            data={'place': _serialize_place(place)},
            message='Place created successfully',
        )

    @staticmethod
    def update(place_id, **kwargs):
        place = PlaceRepository.get_by_id(place_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        if 'name' in kwargs:
            name = kwargs['name']
            if not name or not name.strip():
                return ServiceResponse.validation_error(
                    errors={'name': 'Name is required'},
                    message='Validation failed',
                )
            if PlaceRepository.name_exists(name, exclude_id=place_id):
                return ServiceResponse.error('A place with this name already exists')
            kwargs['name'] = name.strip()

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
    def delete(place_id):
        place = PlaceRepository.get_by_id(place_id)
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
    def list(page=1, per_page=20, place_id=None, status=None):
        qs = TableRepository.get_all().select_related('place')

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
    def get(table_id):
        table = TableRepository.get_by_id(table_id)
        if not table:
            return ServiceResponse.not_found('Table not found')

        return ServiceResponse.success(data={'table': _serialize_table(table)})

    @staticmethod
    def create(place_id, number, capacity=4):
        place = PlaceRepository.get_by_id(place_id)
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
        )

        return ServiceResponse.created(
            data={'table': _serialize_table(table)},
            message='Table created successfully',
        )

    @staticmethod
    def update(table_id, **kwargs):
        table = TableRepository.get_by_id(table_id)
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
            place = PlaceRepository.get_by_id(kwargs['place_id'])
            if not place:
                return ServiceResponse.not_found('Place not found')
            kwargs['place'] = place
            del kwargs['place_id']

        table = TableRepository.update(table, **kwargs)

        return ServiceResponse.success(
            data={'table': _serialize_table(table)},
            message='Table updated successfully',
        )

    @staticmethod
    def delete(table_id):
        table = TableRepository.get_by_id(table_id)
        if not table:
            return ServiceResponse.not_found('Table not found')

        TableRepository.delete(table)

        return ServiceResponse.success(message='Table deleted successfully')

    @staticmethod
    def update_status(table_id, status):
        if status not in VALID_TABLE_STATUSES:
            return ServiceResponse.validation_error(
                errors={'status': f'Must be one of: {", ".join(VALID_TABLE_STATUSES)}'},
                message='Invalid table status',
            )

        table = TableRepository.update_status(table_id, status)
        if not table:
            return ServiceResponse.not_found('Table not found')

        return ServiceResponse.success(
            data={'table': _serialize_table(table)},
            message=f'Table status updated to {status}',
        )

    @staticmethod
    def get_for_place(place_id):
        place = PlaceRepository.get_by_id(place_id)
        if not place:
            return ServiceResponse.not_found('Place not found')

        tables = TableRepository.get_for_place(place_id).select_related('place')
        return ServiceResponse.success(data={
            'place': {'id': place.id, 'name': place.name},
            'tables': [_serialize_table(t) for t in tables],
        })
