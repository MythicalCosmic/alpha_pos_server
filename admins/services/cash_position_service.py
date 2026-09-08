"""Current cash position and simple month-to-date obligation estimates.

The projection is deliberately a read model.  It reuses Treasury, supplier,
payroll, Expense and RecurringCost records and never posts accounting entries.
"""

import calendar
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from admins.models import RecurringCost
from admins.services.profitability_service import (
    ProfitabilityError,
    save_recurring_cost,
    serialize_recurring_cost,
)
from base.financial import FinancialReportingGroup
from base.helpers.response import ServiceResponse
from base.security.permissions import user_has_permission
from base.services.branch_scope import resolve_actor_branch
from base.services.money_control_service import MoneyControlService
from hr.models import Employee, Expense, SalaryPayment
from stock.models import Supplier, SupplierTransaction
from stock.services.supplier_integrity import validate_supplier_ledgers


TASHKENT = ZoneInfo("Asia/Tashkent")
UZS_QUANTUM = Decimal("1")
MANAGED_RECURRING_GROUPS = (
    FinancialReportingGroup.RENT,
    FinancialReportingGroup.UTILITIES,
    FinancialReportingGroup.OPERATING,
    FinancialReportingGroup.TAXES,
)


def _uzs(value):
    return int(Decimal(value or 0).quantize(UZS_QUANTUM, rounding=ROUND_HALF_UP))


def _issue(code, severity, message, *, entity_type=None, entity_id=None,
           amount_uzs=None, details=None):
    row = {
        "code": code,
        "severity": severity,
        "message": message,
        "details": details or {},
    }
    if entity_type:
        row["entity_type"] = entity_type
    if entity_id is not None:
        row["entity_id"] = entity_id
    if amount_uzs is not None:
        row["amount_uzs"] = _uzs(amount_uzs)
    return row


def _month_context(as_of):
    current_start = as_of.replace(day=1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    return {
        "current_start": current_start,
        "current_days": calendar.monthrange(as_of.year, as_of.month)[1],
        "elapsed_days": as_of.day,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "previous_days": previous_end.day,
    }


def _prorate(amount, elapsed_days, divisor_days):
    if amount <= 0 or elapsed_days <= 0:
        return Decimal("0")
    return (
        Decimal(amount) * Decimal(elapsed_days) / Decimal(divisor_days)
    ).quantize(UZS_QUANTUM, rounding=ROUND_HALF_UP)


def _branch_or_error(actor):
    branch_id = str(resolve_actor_branch(actor) or "").strip()
    if branch_id:
        return branch_id, None
    return None, ServiceResponse.failure(
        "BRANCH_SCOPE_REQUIRED",
        "Cash-position branch could not be resolved.",
        403,
    )


class CashPositionService:
    @classmethod
    def get(cls, *, actor):
        branch_id, error = _branch_or_error(actor)
        if error:
            return error

        starts_snapshot = not connection.in_atomic_block
        with transaction.atomic():
            if connection.vendor == "postgresql" and starts_snapshot:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
            now = timezone.now().astimezone(TASHKENT)
            as_of = now.date()
            month = _month_context(as_of)
            treasury, treasury_issues = MoneyControlService._treasury(branch_id)
            supplier_debts, supplier_issues, supplier_blocked = cls._supplier_debts(
                branch_id
            )
            payroll, payroll_issues = cls._payroll(branch_id, as_of, month)
            monthly_costs, cost_issues = cls._monthly_costs(
                branch_id, as_of, month
            )

            issues = treasury_issues + supplier_issues + payroll_issues + cost_issues
            safe = treasury["safe_uzs"]
            bank = treasury["bank_uzs"]
            funds_total = safe + bank if safe is not None and bank is not None else None
            unavailable = funds_total is None or supplier_blocked

            if unavailable:
                after_suppliers = None
                after_payroll = None
                final = None
                status = "UNAVAILABLE"
            else:
                after_suppliers = funds_total - supplier_debts["total_uzs"]
                after_payroll = after_suppliers - payroll["due_estimate_uzs"]
                final = after_payroll - monthly_costs["due_estimate_uzs"]
                status = (
                    "REVIEW_REQUIRED"
                    if any(row["severity"] in ("WARNING", "ERROR") for row in issues)
                    else "ESTIMATED"
                )

            return ServiceResponse.success(data={
                "as_of": now.isoformat(),
                "status": status,
                "currency": "UZS",
                "calculation": {
                    "as_of_date": as_of.isoformat(),
                    "elapsed_days": month["elapsed_days"],
                    "current_month_days": month["current_days"],
                    "previous_month_start": month["previous_start"].isoformat(),
                    "previous_month_end": month["previous_end"].isoformat(),
                    "previous_month_days": month["previous_days"],
                },
                "funds": {
                    "safe_uzs": safe,
                    "bank_uzs": bank,
                    "total_uzs": funds_total,
                },
                "supplier_debts": supplier_debts,
                "payroll": payroll,
                "monthly_costs": monthly_costs,
                "positions": {
                    "after_suppliers_uzs": after_suppliers,
                    "after_payroll_uzs": after_payroll,
                    "final_uzs": final,
                },
                "formula": (
                    "SAFE + BANK - SUPPLIER_DEBTS - ACCRUED_UNPAID_PAYROLL "
                    "- ACCRUED_MONTHLY_COSTS"
                ),
                "data_quality": {
                    "status": (
                        "UNAVAILABLE" if unavailable else
                        ("REVIEW_REQUIRED" if any(
                            row["severity"] in ("WARNING", "ERROR")
                            for row in issues
                        ) else ("PARTIAL" if issues else "COMPLETE"))
                    ),
                    "issues": issues,
                },
                "can_manage_recurring_costs": user_has_permission(
                    actor, "money.control.reconcile"
                ),
            })

    @staticmethod
    def _supplier_debts(branch_id):
        suppliers = list(Supplier.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
        ).order_by("id"))
        evidence = validate_supplier_ledgers(suppliers)
        history_ids = set(SupplierTransaction.objects.filter(
            supplier_id__in=[row.id for row in suppliers],
            is_deleted=False,
        ).values_list("supplier_id", flat=True))

        rows = []
        issues = []
        total = Decimal("0")
        blocked = False
        for supplier in suppliers:
            item = evidence[supplier.id]
            stored = Decimal(item.stored_balance or 0)
            ledger = Decimal(item.ledger_balance or 0)
            potential_payable = max(stored, ledger, Decimal("0"))
            if potential_payable <= 0:
                continue

            if supplier.currency != "UZS":
                blocked = True
                rows.append({
                    "supplier_id": supplier.id,
                    "supplier_name": supplier.name,
                    "amount_uzs": None,
                    "currency": supplier.currency,
                    "evidence_status": "UNSUPPORTED_CURRENCY",
                    "calculation_basis": "EXCLUDED",
                })
                issues.append(_issue(
                    "SUPPLIER_CURRENCY_UNSUPPORTED",
                    "ERROR",
                    "A supplier debt cannot be converted to UZS.",
                    entity_type="Supplier",
                    entity_id=supplier.id,
                    details={"currency": supplier.currency},
                ))
                continue

            values_are_valid = all(
                value.is_finite() and value == value.to_integral_value()
                for value in (stored, ledger)
            )
            if not values_are_valid:
                blocked = True
                rows.append({
                    "supplier_id": supplier.id,
                    "supplier_name": supplier.name,
                    "amount_uzs": None,
                    "currency": "UZS",
                    "evidence_status": "INVALID_AMOUNT",
                    "calculation_basis": "EXCLUDED",
                })
                issues.append(_issue(
                    "SUPPLIER_BALANCE_INVALID",
                    "ERROR",
                    "A supplier balance is not a whole UZS amount.",
                    entity_type="Supplier",
                    entity_id=supplier.id,
                ))
                continue

            if item.valid:
                evidence_status = "VERIFIED"
                basis = "LEDGER_VERIFIED"
            elif supplier.id not in history_ids and stored > 0:
                evidence_status = "OPENING_BALANCE_REVIEW_REQUIRED"
                basis = "CONSERVATIVE_STORED_BALANCE"
                issues.append(_issue(
                    "SUPPLIER_OPENING_BALANCE_REVIEW_REQUIRED",
                    "WARNING",
                    "Stored supplier debt has no supporting transaction history.",
                    entity_type="Supplier",
                    entity_id=supplier.id,
                    amount_uzs=stored,
                    details={
                        "stored_balance_uzs": _uzs(stored),
                        "ledger_balance_uzs": _uzs(ledger),
                    },
                ))
            else:
                evidence_status = "LEDGER_RECONCILIATION_REQUIRED"
                basis = "CONSERVATIVE_MAX_BALANCE"
                issues.append(_issue(
                    "SUPPLIER_LEDGER_RECONCILIATION_REQUIRED",
                    "WARNING",
                    "Supplier ledger and stored balance disagree.",
                    entity_type="Supplier",
                    entity_id=supplier.id,
                    amount_uzs=potential_payable,
                    details={
                        "stored_balance_uzs": _uzs(stored),
                        "ledger_balance_uzs": _uzs(ledger),
                    },
                ))

            total += potential_payable
            rows.append({
                "supplier_id": supplier.id,
                "supplier_name": supplier.name,
                "amount_uzs": _uzs(potential_payable),
                "stored_balance_uzs": _uzs(stored),
                "ledger_balance_uzs": _uzs(ledger),
                "currency": "UZS",
                "evidence_status": evidence_status,
                "calculation_basis": basis,
            })

        rows.sort(key=lambda row: (-(row["amount_uzs"] or 0), row["supplier_id"]))
        return ({
            "total_uzs": None if blocked else _uzs(total),
            "count": len(rows),
            "review_required_count": sum(
                row["evidence_status"] != "VERIFIED" for row in rows
            ),
            "rows": rows,
            "uses_conservative_balance": any(
                row["calculation_basis"].startswith("CONSERVATIVE") for row in rows
            ),
        }, issues, blocked)

    @staticmethod
    def _payroll(branch_id, as_of, month):
        employees = list(Employee.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            is_active=True,
            hire_date__lte=as_of,
            user__is_deleted=False,
        ).order_by("id"))
        employee_ids = [employee.id for employee in employees]
        current_rows = {
            row.employee_id: row
            for row in SalaryPayment.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                employee_id__in=employee_ids,
                period_year=as_of.year,
                period_month=as_of.month,
            ).order_by("employee_id", "id")
        }
        previous_rows = {
            row.employee_id: row
            for row in SalaryPayment.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                employee_id__in=employee_ids,
                period_year=month["previous_start"].year,
                period_month=month["previous_start"].month,
            ).order_by("employee_id", "id")
        }

        accrued = Decimal("0")
        monthly_baseline = Decimal("0")
        source_counts = Counter()
        pending_count = 0
        for employee in employees:
            current = current_rows.get(employee.id)
            previous = previous_rows.get(employee.id)
            if current is not None:
                baseline = Decimal(current.net_amount or 0)
                divisor = month["current_days"]
                source = "CURRENT_PAYROLL"
                if current.status == SalaryPayment.Status.PENDING:
                    pending_count += 1
            elif previous is not None:
                baseline = Decimal(previous.net_amount or 0)
                divisor = month["previous_days"]
                source = "PREVIOUS_MONTH_PAYROLL"
            else:
                baseline = Decimal(employee.base_salary or 0)
                divisor = month["current_days"]
                source = "ACTIVE_EMPLOYEE_BASE_SALARY"

            active_from = max(month["current_start"], employee.hire_date)
            elapsed = max((as_of - active_from).days + 1, 0)
            monthly_baseline += baseline
            accrued += _prorate(baseline, elapsed, divisor)
            source_counts[source] += 1

        paid_current = sum((
            Decimal(row.net_amount or 0)
            for row in SalaryPayment.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                period_year=as_of.year,
                period_month=as_of.month,
                status=SalaryPayment.Status.PAID,
            )
        ), Decimal("0"))
        accrued = accrued.quantize(UZS_QUANTUM, rounding=ROUND_HALF_UP)
        paid_current = paid_current.quantize(UZS_QUANTUM, rounding=ROUND_HALF_UP)
        due = max(accrued - paid_current, Decimal("0"))

        issues = []
        fallback_count = source_counts["ACTIVE_EMPLOYEE_BASE_SALARY"]
        if fallback_count:
            issues.append(_issue(
                "PAYROLL_BASE_SALARY_FALLBACK",
                "INFO",
                "Some employees have no current or previous payroll snapshot.",
                details={"employee_count": fallback_count},
            ))
        if pending_count:
            issues.append(_issue(
                "PAYROLL_ESTIMATE_USES_PENDING_RECORDS",
                "INFO",
                "The estimate includes current payroll records awaiting approval.",
                details={"employee_count": pending_count},
            ))
        return ({
            "monthly_baseline_uzs": _uzs(monthly_baseline),
            "accrued_estimate_uzs": _uzs(accrued),
            "paid_current_period_uzs": _uzs(paid_current),
            "due_estimate_uzs": _uzs(due),
            "employee_count": len(employees),
            "source_counts": dict(source_counts),
            "elapsed_days": month["elapsed_days"],
            "current_month_days": month["current_days"],
            "previous_month_days": month["previous_days"],
        }, issues)

    @staticmethod
    def _monthly_costs(branch_id, as_of, month):
        schedules = list(RecurringCost.objects.filter(
            branch_id=branch_id,
            is_active=True,
            reporting_group__in=MANAGED_RECURRING_GROUPS,
            start_date__lte=as_of,
        ).filter(
            # A null end means the monthly schedule remains active.
            Q(end_date__isnull=True) | Q(end_date__gte=month["current_start"])
        ).order_by("name", "id"))

        rows = []
        fixed_total = Decimal("0")
        accrued_by_group = defaultdict(lambda: Decimal("0"))
        for schedule in schedules:
            active_start = max(month["current_start"], schedule.start_date)
            active_end = min(as_of, schedule.end_date or as_of)
            active_days = max((active_end - active_start).days + 1, 0)
            accrued = _prorate(
                Decimal(schedule.monthly_amount),
                active_days,
                month["current_days"],
            )
            fixed_total += accrued
            accrued_by_group[schedule.reporting_group] += accrued
            rows.append({
                "row_key": f"fixed-{schedule.id}",
                "recurring_cost_id": schedule.id,
                "name": schedule.name,
                "reporting_group": schedule.reporting_group,
                "basis": "FIXED_MONTHLY",
                "monthly_baseline_uzs": _uzs(schedule.monthly_amount),
                "elapsed_days": active_days,
                "divisor_days": month["current_days"],
                "accrued_estimate_uzs": _uzs(accrued),
                "is_active": schedule.is_active,
                "start_date": schedule.start_date.isoformat(),
                "end_date": schedule.end_date.isoformat() if schedule.end_date else None,
            })

        historical_total = Decimal("0")
        historical_count = 0
        has_fixed_utilities = any(
            row.reporting_group == FinancialReportingGroup.UTILITIES
            for row in schedules
        )
        issues = []
        if not has_fixed_utilities:
            grouped = defaultdict(lambda: {"name": "", "amount": Decimal("0")})
            expense_rows = Expense.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                status=Expense.Status.PAID,
                expense_date__gte=month["previous_start"],
                expense_date__lte=month["previous_end"],
                category__reporting_group=FinancialReportingGroup.UTILITIES,
            ).filter(
                Q(treasury_transaction__isnull=False)
                | Q(cashbox_payment__isnull=False)
            ).select_related("category").order_by("id")
            for expense in expense_rows:
                key = expense.category_id or expense.category_code_snapshot or expense.id
                grouped[key]["name"] = (
                    expense.category_name_snapshot
                    or (expense.category.name if expense.category_id else "")
                    or "Utilities"
                )
                grouped[key]["amount"] += Decimal(expense.amount or 0)

            for key, value in grouped.items():
                baseline = value["amount"]
                accrued = _prorate(
                    baseline,
                    month["elapsed_days"],
                    month["previous_days"],
                )
                historical_total += accrued
                accrued_by_group[FinancialReportingGroup.UTILITIES] += accrued
                historical_count += 1
                rows.append({
                    "row_key": f"history-{key}",
                    "recurring_cost_id": None,
                    "name": value["name"],
                    "reporting_group": FinancialReportingGroup.UTILITIES,
                    "basis": "PREVIOUS_MONTH_ACTUAL",
                    "monthly_baseline_uzs": _uzs(baseline),
                    "elapsed_days": month["elapsed_days"],
                    "divisor_days": month["previous_days"],
                    "accrued_estimate_uzs": _uzs(accrued),
                    "is_active": True,
                    "start_date": month["previous_start"].isoformat(),
                    "end_date": month["previous_end"].isoformat(),
                })
            if not grouped:
                issues.append(_issue(
                    "UTILITY_HISTORY_NOT_FOUND",
                    "INFO",
                    "No prior-month utility expense was found; none was estimated.",
                    details={
                        "period_start": month["previous_start"].isoformat(),
                        "period_end": month["previous_end"].isoformat(),
                    },
                ))

        accrued_total = fixed_total + historical_total
        paid_by_group = defaultdict(lambda: Decimal("0"))
        if accrued_by_group:
            current_expenses = Expense.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                status=Expense.Status.PAID,
                expense_date__gte=month["current_start"],
                expense_date__lte=as_of,
                category__reporting_group__in=tuple(accrued_by_group),
            ).filter(
                Q(treasury_transaction__isnull=False)
                | Q(cashbox_payment__isnull=False)
            ).select_related("category").order_by("id")
            for expense in current_expenses:
                paid_by_group[expense.category.reporting_group] += Decimal(
                    expense.amount or 0
                )

        paid_offset = sum((
            min(accrued, paid_by_group[group])
            for group, accrued in accrued_by_group.items()
        ), Decimal("0"))
        due_total = max(accrued_total - paid_offset, Decimal("0"))
        if paid_offset:
            issues.append(_issue(
                "MONTHLY_COST_PAYMENTS_APPLIED",
                "INFO",
                "Verified current-month expenses reduce the remaining estimate.",
                amount_uzs=paid_offset,
            ))
        rows.sort(key=lambda row: (
            row["basis"] != "PREVIOUS_MONTH_ACTUAL",
            row["name"].casefold(),
            row["row_key"],
        ))
        return ({
            "accrued_estimate_uzs": _uzs(accrued_total),
            "paid_current_period_uzs": _uzs(paid_offset),
            "due_estimate_uzs": _uzs(due_total),
            "fixed_accrued_estimate_uzs": _uzs(fixed_total),
            "historical_accrued_estimate_uzs": _uzs(historical_total),
            "fixed_schedule_count": len(schedules),
            "historical_utility_count": historical_count,
            "rows": rows,
        }, issues)

    @staticmethod
    def recurring_costs(*, actor):
        branch_id, error = _branch_or_error(actor)
        if error:
            return error
        rows = RecurringCost.objects.filter(
            branch_id=branch_id,
            reporting_group__in=MANAGED_RECURRING_GROUPS,
        ).order_by("name", "id")
        return ServiceResponse.success(data={
            "items": [serialize_recurring_cost(row) for row in rows],
            "can_manage": user_has_permission(actor, "money.control.reconcile"),
            "reporting_groups": list(MANAGED_RECURRING_GROUPS),
        })

    @staticmethod
    def save_recurring(*, actor, payload, cost_id=None):
        branch_id, error = _branch_or_error(actor)
        if error:
            return error
        if not user_has_permission(actor, "money.control.reconcile"):
            return ServiceResponse.forbidden(
                "Managing cash-position assumptions is not permitted."
            )
        if not isinstance(payload, dict):
            return ServiceResponse.validation_error({
                "body": "A JSON object is required."
            })
        supported_fields = {
            "name", "reporting_group", "monthly_amount", "start_date",
            "end_date", "is_active", "note",
        }
        unsupported = sorted(set(payload) - supported_fields)
        if unsupported:
            return ServiceResponse.validation_error({
                "body": f"Unsupported fields: {', '.join(unsupported)}"
            })

        existing = None
        if cost_id is not None:
            existing = RecurringCost.objects.filter(
                id=cost_id,
                branch_id=branch_id,
                reporting_group__in=MANAGED_RECURRING_GROUPS,
            ).first()
            if existing is None:
                return ServiceResponse.not_found("Recurring cost not found")

        data = dict(payload)
        group = str(data.get(
            "reporting_group",
            existing.reporting_group if existing else FinancialReportingGroup.OPERATING,
        )).upper()
        if group not in MANAGED_RECURRING_GROUPS:
            return ServiceResponse.validation_error({
                "reporting_group": "Use RENT, UTILITIES, OPERATING or TAXES."
            })
        data["reporting_group"] = group
        if existing is None and not data.get("start_date"):
            today = timezone.now().astimezone(TASHKENT).date()
            data["start_date"] = today.replace(day=1).isoformat()

        amount_value = data.get(
            "monthly_amount", existing.monthly_amount if existing else None
        )
        try:
            amount = Decimal(str(amount_value).strip())
        except (InvalidOperation, TypeError, ValueError, AttributeError):
            amount = Decimal("NaN")
        if (
            not amount.is_finite()
            or amount <= 0
            or amount != amount.to_integral_value()
        ):
            return ServiceResponse.validation_error({
                "monthly_amount": "Enter a positive whole UZS amount."
            })
        data["monthly_amount"] = amount

        try:
            saved = save_recurring_cost(
                branch_id, data, actor, cost_id=cost_id
            )
        except ProfitabilityError as exc:
            return ServiceResponse.validation_error(
                exc.errors or {"body": str(exc)},
                message=str(exc),
            )
        return (
            ServiceResponse.success(data={"item": saved}, message="Updated")
            if cost_id is not None
            else ServiceResponse.created(data={"item": saved})
        )
