import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _mark_published(instance):
    type(instance).objects.filter(pk=instance.pk).update(synced_at=timezone.now())
    instance.refresh_from_db()


def test_hard_catalog_requests_preserve_cloud_tombstones(
    settings, category, product, django_capture_on_commit_callbacks,
):
    from admins.services.category_service import AdminCategoryService
    from admins.services.product_service import AdminProductService

    settings.DEPLOYMENT_MODE = 'cloud'
    _mark_published(category)
    _mark_published(product)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        product_result, product_status = AdminProductService.delete_product(
            product.id, hard_delete=True,
        )
        category_result, category_status = AdminCategoryService.delete_category(
            category.id, hard_delete=True,
        )
        product.refresh_from_db()
        category.refresh_from_db()
        assert product.is_deleted and product.synced_at is None
        assert category.is_deleted and category.synced_at is None

    assert product_status == 200, product_result
    assert category_status == 200, category_result
    product.refresh_from_db()
    category.refresh_from_db()
    assert len(callbacks) == 2
    assert product.synced_at is not None
    assert category.synced_at is not None


def test_hard_order_request_preserves_cloud_tombstone(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from admins.services.order_service import AdminOrderService

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory()
    _mark_published(order)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminOrderService.delete_order(
            order.id, hard_delete=True,
        )
        order.refresh_from_db()
        assert order.is_deleted is True
        assert order.synced_at is None

    assert status == 200, result
    order.refresh_from_db()
    assert callbacks
    assert order.synced_at is not None
    assert type(order).objects.filter(pk=order.pk).exists()


def test_category_status_and_reorder_publish_cloud_changes(
    settings, category, django_capture_on_commit_callbacks,
):
    from base.models import Category
    from admins.services.category_service import AdminCategoryService

    settings.DEPLOYMENT_MODE = 'cloud'
    other = Category.objects.create(name='Other category', sort_order=0)
    _mark_published(category)
    _mark_published(other)
    start_versions = (category.sync_version, other.sync_version)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        result, status = AdminCategoryService.update_category_status(
            category.id, 'INACTIVE',
        )
        assert status == 200, result
        result, status = AdminCategoryService.reorder_categories([
            {'id': category.id, 'sort_order': 1},
            {'id': other.id, 'sort_order': 2},
        ])
        assert status == 200, result
        category.refresh_from_db()
        other.refresh_from_db()
        assert category.synced_at is None
        assert other.synced_at is None

    category.refresh_from_db()
    other.refresh_from_db()
    assert len(callbacks) == 3
    assert category.status == 'INACTIVE'
    assert (category.sort_order, other.sort_order) == (1, 2)
    assert category.sync_version == start_versions[0] + 2
    assert other.sync_version == start_versions[1] + 1
    assert category.synced_at is not None
    assert other.synced_at is not None
