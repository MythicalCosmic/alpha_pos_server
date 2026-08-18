from django.db import migrations, models
import django.db.models.deletion


REPORTING_GROUPS = [
    ('INVENTORY_PURCHASE', 'Inventory purchase'),
    ('PAYROLL', 'Payroll'),
    ('RENT', 'Rent'),
    ('UTILITIES', 'Utilities'),
    ('OPERATING', 'Operating expense'),
    ('WASTE_SPOILAGE', 'Waste and spoilage'),
    ('FINANCE_FEES', 'Finance fees'),
    ('DEPRECIATION', 'Depreciation'),
    ('TAXES', 'Taxes'),
    ('CAPITAL_EXPENDITURE', 'Capital expenditure'),
    ('OWNER_DRAW', 'Owner withdrawal'),
    ('NON_BUSINESS', 'Non-business movement'),
    ('OTHER_INCOME', 'Other income'),
    ('REVIEW', 'Needs review'),
]


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('base', '0056_remove_exclusive_shift_device_slot'),
        ('cashbox', '0003_cashboxexpensecategory_reporting_group'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProfitabilityConfiguration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch_id', models.CharField(max_length=50, unique=True)),
                ('reporting_start_date', models.DateField()),
                ('payroll_confirmed_through', models.DateField(blank=True, null=True)),
                ('fixed_costs_confirmed_through', models.DateField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='base.user')),
            ],
            options={'ordering': ['branch_id']},
        ),
        migrations.CreateModel(
            name='ProductCostProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch_id', models.CharField(db_index=True, max_length=50)),
                ('treatment', models.CharField(choices=[('STANDARD', 'Verified standard unit cost'), ('ZERO', 'Explicit zero COGS')], max_length=12)),
                ('standard_unit_cost', models.DecimalField(blank=True, decimal_places=4, max_digits=15, null=True)),
                ('effective_from', models.DateField()),
                ('effective_to', models.DateField(blank=True, null=True)),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cost_profiles', to='base.product')),
                ('verified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='base.user')),
            ],
            options={'ordering': ['product__name', '-effective_from']},
        ),
        migrations.CreateModel(
            name='RecurringCost',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch_id', models.CharField(db_index=True, max_length=50)),
                ('name', models.CharField(max_length=140)),
                ('reporting_group', models.CharField(choices=REPORTING_GROUPS, max_length=32)),
                ('monthly_amount', models.DecimalField(decimal_places=2, max_digits=15)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('note', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='base.user')),
            ],
            options={'ordering': ['name']},
        ),
        migrations.CreateModel(
            name='ProfitAdjustment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch_id', models.CharField(db_index=True, max_length=50)),
                ('effective_date', models.DateField(db_index=True)),
                ('direction', models.CharField(choices=[('EXPENSE', 'Expense'), ('INCOME', 'Income')], max_length=8)),
                ('reporting_group', models.CharField(choices=REPORTING_GROUPS, max_length=32)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=15)),
                ('description', models.CharField(max_length=255)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('APPROVED', 'Approved')], default='DRAFT', max_length=10)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='base.user')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='base.user')),
            ],
            options={'ordering': ['-effective_date', '-id']},
        ),
        migrations.CreateModel(
            name='CashboxExpenseClassification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reporting_group', models.CharField(choices=REPORTING_GROUPS, max_length=32)),
                ('represented_elsewhere', models.BooleanField(default=False, help_text='Exclude from P&L because the same accrual is in HR or another ledger.')),
                ('cash_movement_represented_elsewhere', models.BooleanField(default=False, help_text='Exclude this payout from cash flow because another ledger has the same payment.')),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('classified_at', models.DateTimeField(auto_now=True)),
                ('classified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='base.user')),
                ('expense', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='profitability_classification', to='cashbox.cashboxexpense')),
            ],
        ),
        migrations.CreateModel(
            name='ProfitPeriodClose',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch_id', models.CharField(db_index=True, max_length=50)),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('revision', models.PositiveIntegerField(default=1)),
                ('source_digest', models.CharField(max_length=64)),
                ('report_snapshot', models.JSONField()),
                ('correction_reason', models.CharField(blank=True, default='', max_length=255)),
                ('closed_at', models.DateTimeField(auto_now_add=True)),
                ('closed_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='base.user')),
            ],
            options={'ordering': ['-period_end', '-revision']},
        ),
        migrations.AddConstraint(
            model_name='productcostprofile',
            constraint=models.UniqueConstraint(fields=('branch_id', 'product', 'effective_from'), name='uniq_product_cost_effective_date'),
        ),
        migrations.AddConstraint(
            model_name='productcostprofile',
            constraint=models.CheckConstraint(condition=models.Q(('effective_to__isnull', True), ('effective_to__gte', models.F('effective_from')), _connector='OR'), name='product_cost_valid_effective_range'),
        ),
        migrations.AddConstraint(
            model_name='productcostprofile',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('standard_unit_cost__gt', 0), ('treatment', 'STANDARD')), ('treatment', 'ZERO'), _connector='OR'), name='product_cost_treatment_amount_valid'),
        ),
        migrations.AddConstraint(
            model_name='recurringcost',
            constraint=models.CheckConstraint(condition=models.Q(('monthly_amount__gt', 0)), name='recurring_cost_amount_positive'),
        ),
        migrations.AddConstraint(
            model_name='recurringcost',
            constraint=models.CheckConstraint(condition=models.Q(('end_date__isnull', True), ('end_date__gte', models.F('start_date')), _connector='OR'), name='recurring_cost_valid_date_range'),
        ),
        migrations.AddConstraint(
            model_name='profitadjustment',
            constraint=models.CheckConstraint(condition=models.Q(('amount__gt', 0)), name='profit_adjustment_amount_positive'),
        ),
        migrations.AddConstraint(
            model_name='profitperiodclose',
            constraint=models.UniqueConstraint(fields=('branch_id', 'period_start', 'period_end', 'revision'), name='uniq_profit_period_close_revision'),
        ),
        migrations.AddConstraint(
            model_name='profitperiodclose',
            constraint=models.CheckConstraint(condition=models.Q(('period_end__gte', models.F('period_start'))), name='profit_close_valid_period'),
        ),
    ]
