"""Cloud-owned configuration and audit records for restaurant profitability."""

from django.core.exceptions import ValidationError
from django.db import models

from base.financial import FinancialReportingGroup


class ProfitabilityConfiguration(models.Model):
    """Branch-level activation and evidence-confirmation boundaries."""

    branch_id = models.CharField(max_length=50, unique=True)
    reporting_start_date = models.DateField()
    payroll_confirmed_through = models.DateField(null=True, blank=True)
    fixed_costs_confirmed_through = models.DateField(null=True, blank=True)
    updated_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['branch_id']

    def __str__(self):
        return f'Profitability settings for {self.branch_id}'


class ProductCostProfile(models.Model):
    """Verified, effective-dated fallback when actual stock cost is unavailable."""

    class Treatment(models.TextChoices):
        STANDARD = 'STANDARD', 'Verified standard unit cost'
        ZERO = 'ZERO', 'Explicit zero COGS'

    branch_id = models.CharField(max_length=50, db_index=True)
    product = models.ForeignKey(
        'base.Product', on_delete=models.PROTECT, related_name='cost_profiles',
    )
    treatment = models.CharField(max_length=12, choices=Treatment.choices)
    standard_unit_cost = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True,
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default='')
    verified_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['product__name', '-effective_from']
        constraints = [
            models.UniqueConstraint(
                fields=['branch_id', 'product', 'effective_from'],
                name='uniq_product_cost_effective_date',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(effective_to__isnull=True)
                    | models.Q(effective_to__gte=models.F('effective_from'))
                ),
                name='product_cost_valid_effective_range',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        treatment='STANDARD', standard_unit_cost__gt=0,
                    )
                    | models.Q(treatment='ZERO')
                ),
                name='product_cost_treatment_amount_valid',
            ),
        ]

    def __str__(self):
        return f'{self.product} · {self.treatment} from {self.effective_from}'


class RecurringCost(models.Model):
    """Monthly accrual schedule for rent, utilities, and other fixed costs."""

    branch_id = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=140)
    reporting_group = models.CharField(
        max_length=32, choices=FinancialReportingGroup.choices,
    )
    monthly_amount = models.DecimalField(max_digits=15, decimal_places=2)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(monthly_amount__gt=0),
                name='recurring_cost_amount_positive',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(end_date__isnull=True)
                    | models.Q(end_date__gte=models.F('start_date'))
                ),
                name='recurring_cost_valid_date_range',
            ),
        ]

    def __str__(self):
        return self.name


class ProfitAdjustment(models.Model):
    """Audited one-off accrual that does not already exist in a source ledger."""

    class Direction(models.TextChoices):
        EXPENSE = 'EXPENSE', 'Expense'
        INCOME = 'INCOME', 'Income'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        APPROVED = 'APPROVED', 'Approved'

    branch_id = models.CharField(max_length=50, db_index=True)
    effective_date = models.DateField(db_index=True)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    reporting_group = models.CharField(
        max_length=32, choices=FinancialReportingGroup.choices,
    )
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    description = models.CharField(max_length=255)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT,
    )
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    approved_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-effective_date', '-id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='profit_adjustment_amount_positive',
            ),
        ]

    def __str__(self):
        return f'{self.direction} {self.amount}: {self.description}'


class CashboxExpenseClassification(models.Model):
    """Per-payout override and explicit cross-ledger deduplication evidence."""

    expense = models.OneToOneField(
        'cashbox.CashboxExpense',
        on_delete=models.CASCADE,
        related_name='profitability_classification',
    )
    reporting_group = models.CharField(
        max_length=32, choices=FinancialReportingGroup.choices,
    )
    represented_elsewhere = models.BooleanField(
        default=False,
        help_text='Exclude from P&L because the same accrual is in HR or another ledger.',
    )
    cash_movement_represented_elsewhere = models.BooleanField(
        default=False,
        help_text='Exclude this payout from cash flow because another ledger has the same payment.',
    )
    note = models.CharField(max_length=255, blank=True, default='')
    classified_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    classified_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Cashbox expense {self.expense_id}: {self.reporting_group}'


class ProfitPeriodClose(models.Model):
    """Append-only final snapshot. Corrections create a higher revision."""

    branch_id = models.CharField(max_length=50, db_index=True)
    period_start = models.DateField()
    period_end = models.DateField()
    revision = models.PositiveIntegerField(default=1)
    source_digest = models.CharField(max_length=64)
    report_snapshot = models.JSONField()
    correction_reason = models.CharField(max_length=255, blank=True, default='')
    closed_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='+',
    )
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_end', '-revision']
        constraints = [
            models.UniqueConstraint(
                fields=['branch_id', 'period_start', 'period_end', 'revision'],
                name='uniq_profit_period_close_revision',
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F('period_start')),
                name='profit_close_valid_period',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Closed profitability snapshots are immutable')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Closed profitability snapshots cannot be deleted')

    def __str__(self):
        return (
            f'{self.branch_id} {self.period_start}..{self.period_end} '
            f'r{self.revision}'
        )
