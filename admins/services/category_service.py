from base.repositories import CategoryRepository
from base.helpers.response import ServiceResponse


ALLOWED_ORDER_FIELDS = {
    'sort_order', '-sort_order', 'name', '-name',
    'created_at', '-created_at', 'updated_at', '-updated_at',
    'status', '-status', 'id', '-id',
}


def _serialize_category(cat):
    return {
        'id': cat.id,
        'name': cat.name,
        'slug': cat.slug,
        'description': cat.description,
        'colors': cat.colors,
        'status': cat.status,
        'sort_order': cat.sort_order,
        'is_deleted': cat.is_deleted,
        'created_at': cat.created_at.isoformat() if cat.created_at else None,
        'updated_at': cat.updated_at.isoformat() if cat.updated_at else None,
    }


def _serialize_category_short(cat):
    return {
        'id': cat.id,
        'name': cat.name,
        'slug': cat.slug,
        'description': cat.description,
        'colors': cat.colors,
        'status': cat.status,
        'sort_order': cat.sort_order,
    }


class AdminCategoryService:

    @staticmethod
    def get_all_categories(page=1, per_page=20, search=None, status=None,
                           order_by='sort_order', include_deleted=False):
        if include_deleted:
            queryset = CategoryRepository.model.objects.all()
        else:
            queryset = CategoryRepository.model.objects.filter(is_deleted=False)

        if search:
            queryset = CategoryRepository.search(queryset, search)

        if status:
            queryset = queryset.filter(status=status)

        if order_by not in ALLOWED_ORDER_FIELDS:
            order_by = 'sort_order'
        queryset = queryset.order_by(order_by)

        page_obj, paginator = CategoryRepository.paginate(queryset, page, per_page)

        categories = [_serialize_category(cat) for cat in page_obj.object_list]

        return ServiceResponse.success(data={
            'categories': categories,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_categories': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get_active_categories():
        categories = CategoryRepository.get_active()
        return ServiceResponse.success(data={'categories': list(categories)})

    @staticmethod
    def get_category_by_id(category_id, include_deleted=False):
        if include_deleted:
            category = CategoryRepository.get_by_id_include_deleted(category_id)
        else:
            category = CategoryRepository.get_by_id_cached(category_id)

        if not category:
            return ServiceResponse.not_found("Category not found")

        return ServiceResponse.success(data={'category': _serialize_category(category)})

    @staticmethod
    def get_category_by_slug(slug):
        category = CategoryRepository.get_by_slug(slug)
        if not category:
            return ServiceResponse.not_found("Category not found")

        return ServiceResponse.success(data={
            'category': {
                'id': category.id,
                'name': category.name,
                'slug': category.slug,
                'description': category.description,
                'colors': category.colors,
                'sort_order': category.sort_order,
            }
        })

    @staticmethod
    def create_category(name, description=None, sort_order=0, status='ACTIVE',
                        colors=None, slug=None):
        name = name.strip()

        if CategoryRepository.name_exists(name):
            return ServiceResponse.error("Category with this name already exists")

        if slug:
            if CategoryRepository.slug_exists(slug):
                return ServiceResponse.error("Category with this slug already exists")
        else:
            slug = CategoryRepository.generate_unique_slug(name)

        category = CategoryRepository.create(
            name=name,
            slug=slug,
            description=description or '',
            sort_order=sort_order,
            status=status,
            colors=colors or [],
        )

        CategoryRepository.invalidate_cache()

        return ServiceResponse.created(
            data={'category': _serialize_category_short(category)},
            message="Category created successfully",
        )

    @staticmethod
    def update_category(category_id, **kwargs):
        category = CategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found("Category not found")

        if 'name' in kwargs and kwargs['name']:
            new_name = kwargs['name'].strip()
            if new_name.lower() != category.name.lower():
                if CategoryRepository.name_exists(new_name, exclude_id=category_id):
                    return ServiceResponse.error("Category with this name already exists")
                kwargs['name'] = new_name
                if 'slug' not in kwargs:
                    kwargs['slug'] = CategoryRepository.generate_unique_slug(new_name, exclude_id=category_id)

        if 'slug' in kwargs and kwargs['slug']:
            new_slug = kwargs['slug']
            if new_slug != category.slug:
                if CategoryRepository.slug_exists(new_slug, exclude_id=category_id):
                    return ServiceResponse.error("Category with this slug already exists")

        allowed_fields = {'name', 'slug', 'description', 'status', 'sort_order', 'colors'}
        for key, value in kwargs.items():
            if key in allowed_fields and hasattr(category, key):
                setattr(category, key, value)

        category.save()
        CategoryRepository.invalidate_cache()

        return ServiceResponse.success(
            data={'category': _serialize_category_short(category)},
            message="Category updated successfully",
        )

    @staticmethod
    def delete_category(category_id, hard_delete=False):
        if hard_delete:
            category = CategoryRepository.get_by_id_include_deleted(category_id)
        else:
            category = CategoryRepository.get_by_id(category_id)

        if not category:
            return ServiceResponse.not_found("Category not found")

        if not hard_delete and category.is_deleted:
            return ServiceResponse.error("Category is already deleted")

        product_count = category.products.filter(is_deleted=False).count()
        if product_count > 0:
            return ServiceResponse.error(
                f"Cannot delete category. It has {product_count} active product(s). Move or delete them first."
            )

        if hard_delete:
            category.hard_delete()
            CategoryRepository.invalidate_cache()
            return ServiceResponse.success(message="Category permanently deleted")

        category.is_deleted = True
        category.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])
        CategoryRepository.invalidate_cache()
        return ServiceResponse.success(message="Category deleted successfully")

    @staticmethod
    def restore_category(category_id):
        category = CategoryRepository.get_by_id_include_deleted(category_id)
        if not category:
            return ServiceResponse.not_found("Category not found")

        if not category.is_deleted:
            return ServiceResponse.error("Category is not deleted")

        if CategoryRepository.slug_exists(category.slug):
            category.slug = CategoryRepository.generate_unique_slug(category.name)

        if CategoryRepository.name_exists(category.name):
            category.name = f"{category.name} (restored)"
            category.slug = CategoryRepository.generate_unique_slug(category.name)

        category.is_deleted = False
        category.save()
        CategoryRepository.invalidate_cache()

        return ServiceResponse.success(
            data={
                'category': {
                    'id': category.id,
                    'name': category.name,
                    'slug': category.slug,
                }
            },
            message="Category restored successfully",
        )

    @staticmethod
    def update_category_status(category_id, status):
        if status not in ('ACTIVE', 'INACTIVE'):
            return ServiceResponse.validation_error(
                errors={'status': 'Must be ACTIVE or INACTIVE'},
                message="Invalid status",
            )

        updated = CategoryRepository.model.objects.filter(
            id=category_id, is_deleted=False
        ).update(status=status)

        if updated == 0:
            return ServiceResponse.not_found("Category not found")

        CategoryRepository.invalidate_cache()
        return ServiceResponse.success(message=f"Category status updated to {status}")

    @staticmethod
    def toggle_category_status(category_id):
        category = CategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found("Category not found")

        new_status = 'INACTIVE' if category.status == 'ACTIVE' else 'ACTIVE'
        category.status = new_status
        category.save(update_fields=['status', 'synced_at', 'sync_version'])
        CategoryRepository.invalidate_cache()

        return ServiceResponse.success(
            data={'status': new_status},
            message=f"Category status updated to {new_status}",
        )

    @staticmethod
    def reorder_categories(category_orders):
        if not category_orders or not isinstance(category_orders, list):
            return ServiceResponse.validation_error(
                errors={'orders': 'Invalid category orders data'},
                message="Validation failed",
            )

        for item in category_orders:
            if 'id' not in item or 'sort_order' not in item:
                return ServiceResponse.validation_error(
                    errors={'orders': 'Each item must have id and sort_order'},
                    message="Validation failed",
                )
            CategoryRepository.model.objects.filter(
                id=item['id'], is_deleted=False
            ).update(sort_order=item['sort_order'])

        CategoryRepository.invalidate_cache()
        return ServiceResponse.success(message="Categories reordered successfully")

    @staticmethod
    def get_category_stats():
        stats = CategoryRepository.get_stats()
        return ServiceResponse.success(data=stats)

    @staticmethod
    def get_deleted_categories(page=1, per_page=20):
        queryset = CategoryRepository.get_deleted()
        page_obj, paginator = CategoryRepository.paginate(queryset, page, per_page)

        categories = []
        for cat in page_obj.object_list:
            categories.append({
                'id': cat.id,
                'name': cat.name,
                'slug': cat.slug,
                'updated_at': cat.updated_at.isoformat() if cat.updated_at else None,
            })

        return ServiceResponse.success(data={
            'categories': categories,
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_categories': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def bulk_delete(category_ids):
        if not category_ids or not isinstance(category_ids, list):
            return ServiceResponse.validation_error(
                errors={'ids': 'Invalid category IDs'},
                message="Validation failed",
            )

        categories_with_products = []
        for cat_id in category_ids:
            cat = CategoryRepository.get_by_id(cat_id)
            if cat and cat.products.filter(is_deleted=False).exists():
                categories_with_products.append(cat.name)

        if categories_with_products:
            return ServiceResponse.error(
                f'Cannot delete categories with products: {", ".join(categories_with_products)}'
            )

        deleted = CategoryRepository.bulk_soft_delete(category_ids)
        CategoryRepository.invalidate_cache()

        return ServiceResponse.success(
            data={'deleted_count': deleted},
            message=f"{deleted} category(ies) deleted successfully",
        )

    @staticmethod
    def bulk_restore(category_ids):
        if not category_ids or not isinstance(category_ids, list):
            return ServiceResponse.validation_error(
                errors={'ids': 'Invalid category IDs'},
                message="Validation failed",
            )

        restored = 0
        for cat_id in category_ids:
            result, _ = AdminCategoryService.restore_category(cat_id)
            if result.get('success'):
                restored += 1

        return ServiceResponse.success(
            data={'restored_count': restored},
            message=f"{restored} category(ies) restored successfully",
        )
