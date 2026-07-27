import json
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from base.repositories.session import SessionRepository


pytestmark = pytest.mark.django_db


def _auth(user):
    from base.models import Session

    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address="127.0.0.1",
        user_agent="",
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@override_settings(
    DEPLOYMENT_MODE="cloud",
    BRANCH_ID="cloud",
    CLOUD_DEFAULT_TARGET_BRANCH_ID="branch-a",
)
def test_customer_list_is_paginated_searchable_and_branch_scoped(admin_user):
    from base.models import Customer

    admin_user.branch_id = "branch-a"
    admin_user.save(update_fields=["branch_id"])
    visible = Customer.objects.create(
        name="Nigina Karimova",
        phone_number="+998 90 123-45-67",
        branch_id="branch-a",
    )
    Customer.objects.create(
        name="Foreign",
        phone_number="998901234568",
        branch_id="branch-b",
    )

    response = Client().get(
        "/api/admins/customers",
        {"search": "90 123 45 67", "page": 1, "per_page": 1},
        **_auth(admin_user),
    )

    assert response.status_code == 200, response.content
    payload = response.json()["data"]
    assert payload["total"] == 1
    assert payload["pagination"]["total"] == 1
    assert payload["customers"] == [
        {
            "id": visible.id,
            "name": "Nigina Karimova",
            "phone_number": "998901234567",
            "created_at": visible.created_at.isoformat(),
            "updated_at": visible.updated_at.isoformat(),
        }
    ]


@override_settings(
    DEPLOYMENT_MODE="cloud",
    BRANCH_ID="cloud",
    CLOUD_DEFAULT_TARGET_BRANCH_ID="branch-a",
)
def test_customer_patch_validates_name_phone_duplicates_and_branch(admin_user):
    from base.models import Customer

    admin_user.branch_id = "branch-a"
    admin_user.save(update_fields=["branch_id"])
    customer = Customer.objects.create(
        name="Old Name",
        phone_number="998901234567",
        branch_id="branch-a",
    )
    Customer.objects.create(
        name="Duplicate",
        phone_number="+998 90 000-00-01",
        branch_id="branch-a",
    )
    foreign = Customer.objects.create(
        name="Foreign",
        phone_number="998901234569",
        branch_id="branch-b",
    )
    client = Client()
    auth = _auth(admin_user)

    blank = client.patch(
        f"/api/admins/customers/{customer.id}",
        data=json.dumps({"name": "   "}),
        content_type="application/json",
        **auth,
    )
    assert blank.status_code == 422
    customer.refresh_from_db()
    assert customer.name == "Old Name"

    invalid_phone = client.patch(
        f"/api/admins/customers/{customer.id}",
        data=json.dumps({"phone_number": "123"}),
        content_type="application/json",
        **auth,
    )
    assert invalid_phone.status_code == 422

    duplicate_phone = client.patch(
        f"/api/admins/customers/{customer.id}",
        data=json.dumps({"phone_number": "90 000 00 01"}),
        content_type="application/json",
        **auth,
    )
    assert duplicate_phone.status_code == 422

    foreign_edit = client.patch(
        f"/api/admins/customers/{foreign.id}",
        data=json.dumps({"name": "Leaked"}),
        content_type="application/json",
        **auth,
    )
    assert foreign_edit.status_code == 404

    updated = client.patch(
        f"/api/admins/customers/{customer.id}",
        data=json.dumps(
            {
                "name": "  New Name  ",
                "phone_number": "+998 91 765-43-21",
            }
        ),
        content_type="application/json",
        **auth,
    )
    assert updated.status_code == 200, updated.content
    row = updated.json()["data"]["customer"]
    assert row["name"] == "New Name"
    assert row["phone_number"] == "998917654321"


@override_settings(
    DEPLOYMENT_MODE="cloud",
    BRANCH_ID="cloud",
    CLOUD_DEFAULT_TARGET_BRANCH_ID="branch-a",
)
def test_loyalty_customer_identity_is_exact_unambiguous_and_branch_scoped(
    admin_user,
):
    from base.models import Customer
    from notifications.models import LoyaltyAccount

    admin_user.branch_id = "branch-a"
    admin_user.save(update_fields=["branch_id"])
    account = LoyaltyAccount.objects.create(
        phone_number="998901234567",
        stamps_balance=12,
    )
    matching = Customer.objects.create(
        name="Matching",
        phone_number="+998 90 123-45-67",
        branch_id="branch-a",
    )
    Customer.objects.create(
        name="Foreign",
        phone_number="998901234567",
        branch_id="branch-b",
    )
    missing_account = LoyaltyAccount.objects.create(
        phone_number="998909999999",
        stamps_balance=1,
    )
    Customer.objects.create(
        name="Foreign only",
        phone_number=missing_account.phone_number,
        branch_id="branch-b",
    )
    client = Client()
    auth = _auth(admin_user)

    detail = client.get(
        "/api/admins/notifications/loyalty/accounts/998901234567/",
        **auth,
    )
    assert detail.status_code == 200, detail.content
    assert detail.json()["data"]["customer_id"] == matching.id
    assert detail.json()["data"]["customer_name"] == "Matching"

    listed = client.get(
        "/api/admins/notifications/loyalty/accounts/",
        **auth,
    )
    assert listed.status_code == 200, listed.content
    listed_by_phone = {
        row["phone_number"]: row for row in listed.json()["data"]
    }
    assert listed_by_phone[account.phone_number]["customer_id"] == matching.id
    assert listed_by_phone[missing_account.phone_number]["customer_id"] is None
    assert listed_by_phone[missing_account.phone_number]["customer_name"] is None

    redeemed = client.post(
        "/api/admins/notifications/loyalty/accounts/998901234567/redeem/",
        **auth,
    )
    assert redeemed.status_code == 200, redeemed.content
    assert redeemed.json()["data"]["customer_id"] == matching.id

    # Legacy duplicate rows are ambiguous: never guess which customer owns the
    # loyalty account.
    Customer.objects.create(
        name="Duplicate",
        phone_number="90 123 45 67",
        branch_id="branch-a",
    )
    ambiguous = client.get(
        "/api/admins/notifications/loyalty/accounts/998901234567/",
        **auth,
    )
    assert ambiguous.status_code == 200
    assert ambiguous.json()["data"]["customer_id"] is None
    assert ambiguous.json()["data"]["customer_name"] is None

    # Serializing an account never creates a missing base.Customer.
    assert account.phone_number == "998901234567"
    assert Customer.objects.filter(branch_id="branch-a").count() == 2


@override_settings(
    DEPLOYMENT_MODE="cloud",
    BRANCH_ID="cloud",
    CLOUD_DEFAULT_TARGET_BRANCH_ID="branch-a",
    SYNC_ENABLED=False,
)
def test_admin_order_resolves_customer_only_with_nonblank_name(monkeypatch):
    from admins.services import order_service
    from admins.services.order_service import AdminOrderService
    from base.models import Category, Customer, Order, Product, Shift, User

    owner = User.objects.create(
        email="owner@example.test",
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
        password="!",
        branch_id="cloud",
    )
    cashier = User.objects.create(
        email="cashier@example.test",
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        password="!",
        branch_id="cloud",
    )
    Shift.objects.create(
        user=cashier,
        start_time=timezone.now(),
        status=Shift.Status.ACTIVE,
        branch_id="branch-a",
    )
    category = Category.objects.create(name="Food", branch_id="cloud")
    product = Product.objects.create(
        name="Meal",
        price=Decimal("50000"),
        category=category,
        branch_id="cloud",
    )
    monkeypatch.setattr(
        order_service,
        "_apply_order_stock_transition",
        lambda *args, **kwargs: None,
    )
    items = [{"product_id": product.id, "quantity": 1}]

    created, status = AdminOrderService.create_order(
        owner.id,
        items,
        cashier_id=cashier.id,
        phone_number="+998 90 123-45-67",
        customer_name="  Nigina  ",
    )
    assert status == 201, created
    customer = Customer.objects.get(
        branch_id="branch-a",
        phone_number="998901234567",
    )
    assert customer.name == "Nigina"
    assert Order.objects.get(pk=created["data"]["order_id"]).customer == customer

    blank, blank_status = AdminOrderService.create_order(
        owner.id,
        items,
        cashier_id=cashier.id,
        phone_number="90 123 45 67",
        customer_name="   ",
    )
    assert blank_status == 201, blank
    customer.refresh_from_db()
    assert customer.name == "Nigina"
    assert Customer.objects.filter(
        branch_id="branch-a",
        phone_number="998901234567",
    ).count() == 1
