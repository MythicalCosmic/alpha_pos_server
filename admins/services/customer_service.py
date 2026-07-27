"""Branch-scoped customer management for the Admin Panel."""

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q

from base.helpers.response import ServiceResponse
from base.models import Customer
from base.services.branch_scope import resolve_actor_branch
from base.services.phone import is_canonical_uz_phone, normalize_uz_phone


def _serialize(customer):
    return {
        "id": customer.id,
        "name": customer.name,
        "phone_number": customer.phone_number,
        "created_at": customer.created_at.isoformat(),
        "updated_at": customer.updated_at.isoformat(),
    }


def _branch_or_error(actor):
    branch_id = resolve_actor_branch(actor)
    if branch_id:
        return branch_id, None
    return None, ServiceResponse.validation_error(
        errors={"branch_id": "No single authorized branch could be resolved"},
        message="Choose an operational branch",
    )


def _clean_name(value):
    if not isinstance(value, str):
        return None, "Name must be a string"
    name = value.strip()
    if not name:
        return None, "Name must not be blank"
    if len(name) > 120:
        return None, "Name must be 120 characters or fewer"
    if any(ord(character) < 32 for character in name):
        return None, "Name contains unsupported control characters"
    return name, None


class AdminCustomerService:
    @staticmethod
    def list_customers(*, actor, page=1, per_page=20, search=""):
        branch_id, error = _branch_or_error(actor)
        if error:
            return error

        queryset = Customer.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
        )
        search = str(search or "").strip()
        if search:
            normalized = normalize_uz_phone(search)
            predicate = Q(name__icontains=search) | Q(
                phone_number__icontains=search
            )
            if normalized and normalized != search:
                predicate |= Q(phone_number__icontains=normalized)
            queryset = queryset.filter(predicate)

        paginator = Paginator(queryset.order_by("-updated_at", "-id"), per_page)
        page_obj = paginator.get_page(page)
        customers = [_serialize(customer) for customer in page_obj.object_list]
        pagination = {
            "page": page_obj.number,
            "per_page": per_page,
            "total": paginator.count,
            "total_pages": paginator.num_pages,
            "has_next": page_obj.has_next(),
            "has_previous": page_obj.has_previous(),
        }
        return ServiceResponse.success(
            data={
                "customers": customers,
                # Keep the requested top-level count alongside normal pagination
                # metadata so the Admin Panel does not have to infer it.
                "total": paginator.count,
                "pagination": pagination,
            }
        )

    @staticmethod
    @transaction.atomic
    def update_customer(customer_id, *, actor, changes):
        branch_id, error = _branch_or_error(actor)
        if error:
            return error

        customer = (
            Customer.objects.select_for_update()
            .filter(
                id=customer_id,
                branch_id=branch_id,
                is_deleted=False,
            )
            .first()
        )
        if customer is None:
            # A foreign-branch id is deliberately indistinguishable from a
            # missing id; this prevents cross-branch customer enumeration.
            return ServiceResponse.not_found("Customer not found")

        allowed = {"name", "phone_number"}
        supplied = allowed & set(changes)
        if not supplied:
            return ServiceResponse.validation_error(
                errors={
                    "fields": "Provide at least one of name or phone_number"
                },
                message="No editable customer fields were supplied",
            )

        update_fields = []
        if "name" in supplied:
            name, name_error = _clean_name(changes.get("name"))
            if name_error:
                return ServiceResponse.validation_error(
                    errors={"name": name_error}
                )
            customer.name = name
            update_fields.append("name")

        if "phone_number" in supplied:
            raw_phone = changes.get("phone_number")
            if not isinstance(raw_phone, str):
                return ServiceResponse.validation_error(
                    errors={"phone_number": "Phone number must be a string"}
                )
            phone = normalize_uz_phone(raw_phone)
            if not is_canonical_uz_phone(phone):
                return ServiceResponse.validation_error(
                    errors={
                        "phone_number": (
                            "Enter a valid Uzbekistan phone number"
                        )
                    }
                )
            duplicate = Customer.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                phone_number=phone,
            ).exclude(id=customer.id)
            if duplicate.exists():
                return ServiceResponse.validation_error(
                    errors={
                        "phone_number": (
                            "A customer with this phone number already exists"
                        )
                    }
                )
            customer.phone_number = phone
            update_fields.append("phone_number")

        customer.save(update_fields=update_fields)
        return ServiceResponse.success(
            data={"customer": _serialize(customer)},
            message="Customer updated successfully",
        )
