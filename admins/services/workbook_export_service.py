"""Native XLSX exports for the admin dashboard and shift handover report.

The analytics services remain the source of truth.  This module only renders
their already-filtered dictionaries, so a spreadsheet cannot drift from the
numbers displayed by the JSON endpoints.
"""
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter


XLSX_CONTENT_TYPE = (
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
)

_NAVY = '17324D'
_TEAL = '18A7A0'
_WHITE = 'FFFFFF'
_GRID = Side(style='thin', color='D9E2E8')
_FORMULA_PREFIXES = ('=', '+', '-', '@')
_PLAIN_NUMBER = re.compile(r'^-?\d+(?:\.\d+)?$')
_ILLEGAL_XML_CHARS = re.compile(
    r'[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]'
)


def _excel_value(value, *, numeric=False):
    """Return an Excel scalar while preventing formula injection.

    Numeric conversion is opt-in because identifiers and user-controlled names
    may legitimately look numeric (for example ``"001"``).  Converting every
    digit-only string would lose leading zeroes and could round long text IDs.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (Decimal, float)):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()

    text = str(value)
    if numeric:
        stripped = text.strip()
        number = None
        # Never feed arbitrary/exponential user text to Decimal/int.  Besides
        # preserving identifiers, the length and grammar bound prevents values
        # such as ``1e999999999`` from becoming a spreadsheet-export DoS.
        if len(stripped) <= 40 and _PLAIN_NUMBER.fullmatch(stripped):
            try:
                number = Decimal(stripped)
            except (InvalidOperation, TypeError, ValueError):
                number = None
        if number is not None and number.is_finite() and stripped:
            if number == number.to_integral_value():
                return int(number)
            return float(number)

    text = _ILLEGAL_XML_CHARS.sub('', text)
    if text.lstrip().startswith(_FORMULA_PREFIXES):
        text = "'" + text
    # Excel cells are limited to 32,767 characters.  Bound notes/reasons here
    # so a single oversized text value cannot break or bloat the workbook.
    return text[:32767]


def _label(value):
    return str(value).replace('_', ' ').replace('.', ' / ').strip().title()


def _write_title(sheet, title, subtitle='', *, width=8):
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    cell = sheet.cell(1, 1, _excel_value(title))
    cell.font = Font(size=18, bold=True, color=_WHITE)
    cell.fill = PatternFill('solid', fgColor=_NAVY)
    cell.alignment = Alignment(vertical='center')
    sheet.row_dimensions[1].height = 30
    if subtitle:
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
        sub = sheet.cell(2, 1, _excel_value(subtitle))
        sub.font = Font(size=10, italic=True, color='526575')
    sheet.sheet_view.showGridLines = False


def _style_header(cell):
    cell.font = Font(bold=True, color=_WHITE)
    cell.fill = PatternFill('solid', fgColor=_TEAL)
    cell.alignment = Alignment(horizontal='center', vertical='center')
    cell.border = Border(bottom=_GRID)


def _write_table(
        sheet, start_row, headers, rows, *, freeze=True, numeric_columns=()):
    headers = list(headers)
    rows = list(rows)
    numeric_columns = set(numeric_columns)
    for col, header in enumerate(headers, start=1):
        _style_header(sheet.cell(start_row, col, _label(header)))

    for row_offset, row in enumerate(rows, start=1):
        if isinstance(row, Mapping):
            values = [row.get(header) for header in headers]
        else:
            values = list(row)
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(
                start_row + row_offset,
                col,
                _excel_value(value, numeric=headers[col - 1] in numeric_columns),
            )
            cell.border = Border(bottom=_GRID)
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                cell.number_format = '#,##0.##'
        if row_offset % 2 == 0:
            for col in range(1, len(headers) + 1):
                sheet.cell(start_row + row_offset, col).fill = PatternFill(
                    'solid', fgColor='F6FAFC',
                )

    end_row = start_row + max(len(rows), 1)
    if headers:
        sheet.auto_filter.ref = (
            f'A{start_row}:{get_column_letter(len(headers))}{end_row}'
        )
    if freeze:
        sheet.freeze_panes = f'A{start_row + 1}'
    _fit_columns(sheet, len(headers), end_row)
    return end_row


def _fit_columns(sheet, column_count, end_row):
    for column in range(1, column_count + 1):
        values = (
            sheet.cell(row, column).value
            for row in range(1, end_row + 1)
        )
        length = max((len(str(value or '')) for value in values), default=10)
        sheet.column_dimensions[get_column_letter(column)].width = min(
            max(length + 2, 12), 42,
        )


def _flatten(mapping, prefix=''):
    rows = []
    for key, value in (mapping or {}).items():
        path = f'{prefix}.{key}' if prefix else str(key)
        if isinstance(value, Mapping):
            rows.extend(_flatten(value, path))
        elif isinstance(value, (list, tuple)):
            rows.append((path, ', '.join(str(item) for item in value)))
        else:
            rows.append((path, value))
    return rows


def _sheet_with_table(
        workbook, name, title, headers, rows, *, subtitle='', width=None,
        numeric_columns=()):
    sheet = workbook.create_sheet(name)
    width = width or max(len(headers), 2)
    _write_title(sheet, title, subtitle, width=width)
    _write_table(
        sheet, 4, headers, rows, numeric_columns=numeric_columns,
    )
    return sheet


def _dynamic_headers(rows, preferred=()):
    headers = list(preferred)
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers


def _workbook_bytes(workbook):
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _generated_label(generated_at=None):
    generated_at = generated_at or timezone.now()
    if timezone.is_aware(generated_at):
        generated_at = timezone.localtime(generated_at)
    return generated_at.isoformat(timespec='seconds')


def _metric_value(metric, value):
    """Keep human text as text; make analytics scalar strings numeric."""
    lowered = metric.lower()
    text_suffixes = (
        '.name', '.user_name', '.status', '_time', '_at', '.notes',
        '.reconciled_by',
    )
    if lowered.endswith(text_suffixes):
        return _excel_value(value)
    return _excel_value(value, numeric=True)


def _settlement_rows_for_export(rows):
    """Render omitted tender counts as missing evidence, never as shortages."""
    rendered = []
    for source in rows or ():
        row = dict(source)
        count_unsubmitted = (
            row.get('cashier_count_submitted') is False
            or str(row.get('cashier_count_status') or '').upper()
            == 'UNCOUNTED'
            or str(row.get('status') or '').upper() == 'UNCOUNTED'
        )
        if count_unsubmitted:
            row['counted'] = None
            row['difference'] = None
        if not row.get('reconciled') and (
            str(row.get('status') or '').upper() != 'CONFIRMED'
        ):
            row['confirmed'] = None
        rendered.append(row)
    return rendered


def build_dashboard_workbook(data, *, filters=None, generated_at=None):
    """Render the exact ``dashboard_service.get_range`` result to XLSX."""
    filters = filters or {}
    date_range = data.get('range') or {}
    date_from = date_range.get('from') or ''
    date_to = date_range.get('to') or ''
    subtitle = f'Business dates {date_from} to {date_to}'

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = 'Alpha POS'
    workbook.properties.title = 'Dashboard export'
    workbook.properties.subject = _excel_value(subtitle)

    summary = workbook.create_sheet('Summary')
    _write_title(summary, 'Alpha POS Dashboard', subtitle, width=4)
    summary_rows = [
        ('Generated at', _generated_label(generated_at)),
        ('Business date from', date_from),
        ('Business date to', date_to),
        ('Time from', filters.get('tod_from') or 'All day'),
        ('Time to', filters.get('tod_to') or 'All day'),
        ('Net revenue (UZS)', _excel_value(data.get('revenue', 0), numeric=True)),
        ('Gross revenue (UZS)', _excel_value(data.get('gross_revenue', 0), numeric=True)),
        ('Refund amount (UZS)', _excel_value(data.get('refund_amount', 0), numeric=True)),
        ('Orders', data.get('orders', 0)),
        ('Paid orders', data.get('paid_orders', 0)),
        ('Refunded orders', data.get('refunded_orders', 0)),
        ('Cancelled orders', data.get('cancelled', 0)),
        ('Units sold', data.get('units_sold', 0)),
    ]
    _write_table(summary, 4, ('metric', 'value'), summary_rows, freeze=False)

    payment = data.get('payment_breakdown') or {}
    payment_rows = [
        {'method': method, 'amount_uzs': amount}
        for method, amount in payment.items()
        if method != 'card_detail'
    ]
    _sheet_with_table(
        workbook,
        'Payments',
        'Payment Breakdown',
        ('method', 'amount_uzs'),
        payment_rows,
        subtitle=subtitle,
        numeric_columns=('amount_uzs',),
    )
    card_detail_rows = [
        {'acquirer': method, 'amount_uzs': amount}
        for method, amount in (payment.get('card_detail') or {}).items()
    ]
    _sheet_with_table(
        workbook,
        'Card Details',
        'Card Acquirer Detail',
        ('acquirer', 'amount_uzs'),
        card_detail_rows,
        subtitle=f'{subtitle} / components of the card total',
        numeric_columns=('amount_uzs',),
    )

    product_headers = (
        'product_id', 'product_name', 'quantity', 'revenue',
        'gross_quantity', 'refunded_quantity', 'gross_revenue',
        'refund_amount',
    )
    _sheet_with_table(
        workbook,
        'Top Products',
        'Top Products',
        product_headers,
        data.get('top_products') or [],
        subtitle=subtitle,
        numeric_columns={
            'product_id', 'quantity', 'revenue', 'gross_quantity',
            'refunded_quantity', 'gross_revenue', 'refund_amount',
        },
    )

    category_headers = (
        'category_id', 'category', 'quantity', 'revenue',
        'gross_quantity', 'refunded_quantity', 'gross_revenue',
        'refund_amount',
    )
    _sheet_with_table(
        workbook,
        'Categories',
        'Category Performance',
        category_headers,
        data.get('category_stats') or [],
        subtitle=subtitle,
        numeric_columns={
            'category_id', 'quantity', 'revenue', 'gross_quantity',
            'refunded_quantity', 'gross_revenue', 'refund_amount',
        },
    )
    return _workbook_bytes(workbook)


def build_shift_report_workbook(report, *, generated_at=None):
    """Render one canonical ``shift_handover_report`` result to XLSX."""
    shift = report.get('shift') or {}
    distribution = report.get('distribution') or {}
    unbucketed_refunds = (
        distribution.get('unbucketed_refund_adjustment') or {}
    )
    shift_id = shift.get('shift_id') or ''
    cashier = report.get('cashier') or {}
    subtitle = f'Shift {shift_id} / {cashier.get("name") or "Unknown cashier"}'

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = 'Alpha POS'
    workbook.properties.title = f'Shift {shift_id} handover report'
    workbook.properties.subject = _excel_value(subtitle)

    summary = workbook.create_sheet('Summary')
    _write_title(summary, 'Alpha POS Shift Report', subtitle, width=4)
    summary_rows = [
        ('generated_at', _generated_label(generated_at)),
        ('cashier.id', cashier.get('id')),
        ('cashier.name', cashier.get('name')),
        # Stable alias for spreadsheet automations built against older exports.
        ('receipt_count', report.get('receipt_count', 0)),
        ('orders_taken_count', report.get('receipt_count', 0)),
        (
            'orders_paid_in_shift_count',
            report.get('settled_receipt_count', 0),
        ),
        ('peak_hour', report.get('peak_hour')),
        ('distribution.revenue_total', distribution.get('revenue_total')),
        *(
            _flatten(
                unbucketed_refunds,
                'distribution.unbucketed_refund_adjustment',
            )
        ),
        *_flatten(shift, 'shift'),
        *_flatten(report.get('best_seller') or {}, 'best_seller'),
    ]
    summary_rows = [
        (metric, _metric_value(metric, value))
        for metric, value in summary_rows
    ]
    _write_table(summary, 4, ('metric', 'value'), summary_rows, freeze=False)

    sections = (
        (
            'Settlement',
            'Tender Settlement',
            _settlement_rows_for_export(report.get('settlement')),
            (
                'method', 'expected', 'counted', 'confirmed', 'difference',
                'status',
            ),
            {'expected', 'counted', 'confirmed', 'difference'},
        ),
        (
            'Cash Expenses',
            'Cash Expenses',
            report.get('cash_expenses') or [],
            ('category', 'total', 'count'),
            {'total', 'count'},
        ),
        (
            'Receipts',
            'Orders Taken During Shift (created_at)',
            report.get('receipts') or [],
            (
                'order_id', 'display_id', 'status', 'order_type', 'is_paid',
                'payment_method', 'total_amount', 'discount_amount',
                'discount_percent', 'line_items', 'units', 'created_at',
                'paid_at',
            ),
            {
                'order_id', 'display_id', 'total_amount', 'discount_amount',
                'discount_percent', 'line_items', 'units',
            },
        ),
        (
            'Settled Receipts',
            'Orders Paid During Shift (paid_at; drives money totals)',
            report.get('settled_receipts') or [],
            (
                'order_id', 'display_id', 'status', 'order_type',
                'payment_action_id', 'payment_method', 'total_amount',
                'cash_amount', 'card_amount', 'payme_amount',
                'unknown_amount', 'drawer_cash_amount', 'uzcard_amount',
                'humo_amount', 'generic_card_amount',
                'has_concrete_payment_evidence',
                'tender_attribution_complete', 'created_in_this_shift',
                'created_at', 'paid_at',
            ),
            {
                'order_id', 'display_id', 'total_amount', 'cash_amount',
                'card_amount', 'payme_amount', 'unknown_amount',
                'drawer_cash_amount', 'uzcard_amount', 'humo_amount',
                'generic_card_amount',
            },
        ),
        (
            'Refunds',
            'Refunds',
            report.get('refunds') or [],
            (
                'refund_id', 'order_id', 'amount', 'cash', 'card', 'payme',
                'refunded_at', 'reason',
            ),
            {'refund_id', 'order_id', 'amount', 'cash', 'card', 'payme'},
        ),
        (
            'Products',
            'Product Performance',
            report.get('products') or [],
            (
                'product_id', 'name', 'units_sold', 'times_sold',
                'times_refunded', 'revenue',
            ),
            {
                'product_id', 'units_sold', 'times_sold', 'times_refunded',
                'revenue',
            },
        ),
    )
    for name, title, rows, preferred, numeric_columns in sections:
        headers = _dynamic_headers(rows, preferred)
        _sheet_with_table(
            workbook,
            name,
            title,
            headers,
            rows,
            subtitle=subtitle,
            numeric_columns=numeric_columns,
        )

    for key, name, title, preferred in (
        ('by_hour', 'Hourly', 'Hourly Distribution', ('hour', 'orders', 'revenue')),
        ('by_date', 'Daily', 'Daily Distribution', ('date', 'orders', 'revenue')),
    ):
        rows = distribution.get(key) or []
        headers = _dynamic_headers(rows, preferred)
        _sheet_with_table(
            workbook,
            name,
            title,
            headers,
            rows,
            subtitle=subtitle,
            numeric_columns={
                header for header in headers
                if header not in {'date', 'weekday', 'label'}
            },
        )

    return _workbook_bytes(workbook)


def _report_number(value):
    """Convert a canonical decimal string to a typed spreadsheet number."""
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    if not number.is_finite():
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def _add_excel_table(sheet, name, start_row, end_row, column_count):
    if end_row <= start_row:
        return
    ref = (
        f'A{start_row}:{get_column_letter(column_count)}{end_row}'
    )
    table = Table(displayName=name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name='TableStyleMedium2',
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)


def build_product_performance_workbook(report):
    """Render the canonical product performance dataset as a polished XLSX."""
    date_range = report.get('range') or {}
    date_from = date_range.get('from') or ''
    date_to = date_range.get('to') or ''
    subtitle = (
        f'Business dates {date_from} to {date_to} · '
        f'{report.get("currency", "UZS")} · {report.get("branch_id", "")}'
    )
    summary_data = report.get('summary') or {}
    product_rows = report.get('products') or []

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = 'Alpha POS'
    workbook.properties.title = 'Product performance report'
    workbook.properties.subject = _excel_value(subtitle)
    workbook.calculation.fullCalcOnLoad = True

    summary = workbook.create_sheet('Summary')
    _write_title(summary, 'Alpha POS · Product Performance', subtitle, width=8)
    metrics = (
        ('Products', summary_data.get('product_count', 0), '0'),
        ('Units sold', summary_data.get('total_units_sold', 0), '#,##0'),
        ('Net revenue', summary_data.get('total_revenue'), '#,##0.00'),
        ('Ingredient cost', summary_data.get('total_ingredient_cost'), '#,##0.00'),
        ('Gross profit', summary_data.get('total_gross_profit'), '#,##0.00'),
        ('Gross margin', summary_data.get('gross_profit_margin_pct'), '0.00"%"'),
        ('Orders', summary_data.get('order_count', 0), '#,##0'),
        ('Cost coverage', summary_data.get('cost_coverage_pct'), '0.00"%"'),
    )
    for index, (label, value, number_format) in enumerate(metrics):
        row = 4 + (index // 4) * 3
        column = 1 + (index % 4) * 2
        summary.merge_cells(
            start_row=row, start_column=column,
            end_row=row, end_column=column + 1,
        )
        title_cell = summary.cell(row, column, label)
        title_cell.fill = PatternFill('solid', fgColor='E8F3F4')
        title_cell.font = Font(size=10, bold=True, color='355465')
        title_cell.alignment = Alignment(horizontal='center')
        summary.merge_cells(
            start_row=row + 1, start_column=column,
            end_row=row + 1, end_column=column + 1,
        )
        value_cell = summary.cell(row + 1, column, _report_number(value))
        value_cell.font = Font(size=16, bold=True, color=_NAVY)
        value_cell.alignment = Alignment(horizontal='center')
        value_cell.number_format = number_format
        for card_row in (row, row + 1):
            for card_column in (column, column + 1):
                summary.cell(card_row, card_column).border = Border(
                    left=_GRID, right=_GRID, top=_GRID, bottom=_GRID,
                )

    filters = report.get('filters') or {}
    filter_rows = [
        ('Generated at', report.get('generated_at')),
        ('Preset', date_range.get('preset')),
        ('Window start', date_range.get('start_at')),
        ('Window end (exclusive)', date_range.get('end_at')),
        ('Category ID', filters.get('category_id') or 'All'),
        ('Product ID', filters.get('product_id') or 'All'),
        ('Search', filters.get('search') or 'All'),
        ('Cashier ID', filters.get('cashier_id') or 'All'),
        ('Order type', filters.get('order_type') or 'All'),
        ('Order origin', filters.get('order_origin') or 'All'),
        ('Payment method', filters.get('payment_method') or 'All'),
        ('Sort', filters.get('sort')),
        ('Cost complete', summary_data.get('cost_complete')),
        ('Products missing cost', summary_data.get('products_missing_cost', 0)),
    ]
    _write_table(summary, 11, ('report detail', 'value'), filter_rows, freeze=False)
    summary.column_dimensions['A'].width = 27
    summary.column_dimensions['B'].width = 34
    for column in 'CDEFGH':
        summary.column_dimensions[column].width = 15

    headers = (
        'rank', 'product_id', 'product_name', 'category_name',
        'units_sold', 'units_refunded', 'net_units', 'orders_sold',
        'selling_price_per_unit', 'minimum_selling_price',
        'maximum_selling_price', 'current_catalog_price',
        'gross_sales_revenue', 'refund_amount', 'total_revenue',
        'ingredient_cost_per_unit', 'gross_ingredient_cost',
        'ingredient_cost_credit', 'total_ingredient_cost',
        'gross_profit_per_item', 'gross_profit',
        'gross_profit_margin_pct', 'cost_source', 'cost_coverage_pct',
        'cost_complete',
    )
    money_columns = {
        'selling_price_per_unit', 'minimum_selling_price',
        'maximum_selling_price', 'current_catalog_price',
        'gross_sales_revenue', 'refund_amount', 'total_revenue',
        'ingredient_cost_per_unit', 'gross_ingredient_cost',
        'ingredient_cost_credit', 'total_ingredient_cost',
        'gross_profit_per_item', 'gross_profit',
    }
    integer_columns = {
        'rank', 'product_id', 'units_sold', 'units_refunded', 'net_units',
        'orders_sold',
    }
    percent_columns = {'gross_profit_margin_pct', 'cost_coverage_pct'}
    typed_products = []
    for source in product_rows:
        row = dict(source)
        for column in money_columns | percent_columns:
            row[column] = _report_number(row.get(column))
        typed_products.append(row)

    products = workbook.create_sheet('Products')
    _write_title(products, 'Product Performance', subtitle, width=len(headers))
    product_end = _write_table(
        products, 4, headers, typed_products,
        numeric_columns=money_columns | integer_columns | percent_columns,
    )
    _add_excel_table(products, 'ProductPerformance', 4, product_end, len(headers))
    header_indexes = {header: index + 1 for index, header in enumerate(headers)}
    for row in range(5, product_end + 1):
        for column in money_columns:
            products.cell(row, header_indexes[column]).number_format = '#,##0.00'
        for column in percent_columns:
            products.cell(row, header_indexes[column]).number_format = '0.00"%"'
        products.cell(row, header_indexes['product_name']).alignment = Alignment(
            vertical='top', wrap_text=False,
        )
    if product_rows:
        for field in ('gross_profit', 'gross_profit_margin_pct'):
            column = get_column_letter(header_indexes[field])
            products.conditional_formatting.add(
                f'{column}5:{column}{product_end}',
                ColorScaleRule(
                    start_type='min', start_color='FECACA',
                    mid_type='percentile', mid_value=50, mid_color='FEF3C7',
                    end_type='max', end_color='BBF7D0',
                ),
            )
    total_row = product_end + 2
    products.cell(total_row, 1, 'TOTAL')
    products.cell(total_row, 1).font = Font(bold=True, color=_WHITE)
    for column in range(1, len(headers) + 1):
        products.cell(total_row, column).fill = PatternFill('solid', fgColor=_NAVY)
        products.cell(total_row, column).font = Font(bold=True, color=_WHITE)
    total_values = {
        'units_sold': summary_data.get('total_units_sold'),
        'units_refunded': summary_data.get('total_units_refunded'),
        'net_units': summary_data.get('net_units'),
        'gross_sales_revenue': summary_data.get('gross_sales_revenue'),
        'refund_amount': summary_data.get('refund_amount'),
        'total_revenue': summary_data.get('total_revenue'),
        'gross_ingredient_cost': summary_data.get('gross_ingredient_cost'),
        'ingredient_cost_credit': summary_data.get('ingredient_cost_credit'),
        'total_ingredient_cost': summary_data.get('total_ingredient_cost'),
        'gross_profit': summary_data.get('total_gross_profit'),
        'gross_profit_margin_pct': summary_data.get('gross_profit_margin_pct'),
        'cost_coverage_pct': summary_data.get('cost_coverage_pct'),
        'cost_complete': summary_data.get('cost_complete'),
    }
    for field, value in total_values.items():
        cell = products.cell(total_row, header_indexes[field], _report_number(value))
        cell.fill = PatternFill('solid', fgColor=_NAVY)
        cell.font = Font(bold=True, color=_WHITE)
        if field in money_columns:
            cell.number_format = '#,##0.00'
        elif field in percent_columns:
            cell.number_format = '0.00"%"'
    products.auto_filter.ref = f'A4:{get_column_letter(len(headers))}{product_end}'
    products.freeze_panes = 'E5'
    products.sheet_properties.pageSetUpPr.fitToPage = True
    products.page_setup.orientation = 'landscape'
    products.page_setup.fitToWidth = 1
    products.page_setup.fitToHeight = 0
    products.print_title_rows = '1:4'

    if product_rows:
        chart_end = min(product_end, 14)
        chart = BarChart()
        chart.type = 'bar'
        chart.style = 10
        chart.title = 'Top products · net revenue and gross profit'
        chart.y_axis.title = 'Product'
        chart.x_axis.title = 'UZS'
        chart.height = 8.5
        chart.width = 16
        chart.add_data(
            Reference(
                products,
                min_col=header_indexes['total_revenue'],
                min_row=4,
                max_row=chart_end,
            ),
            titles_from_data=True,
        )
        chart.add_data(
            Reference(
                products,
                min_col=header_indexes['gross_profit'],
                min_row=4,
                max_row=chart_end,
            ),
            titles_from_data=True,
        )
        chart.set_categories(
            Reference(
                products,
                min_col=header_indexes['product_name'],
                min_row=5,
                max_row=chart_end,
            )
        )
        chart.legend.position = 'b'
        summary.add_chart(chart, 'D11')

    category_headers = (
        'category_id', 'category_name', 'product_count', 'orders',
        'units_sold', 'units_refunded', 'net_units', 'gross_sales_revenue',
        'refund_amount', 'total_revenue', 'total_ingredient_cost',
        'gross_profit', 'gross_profit_margin_pct', 'cost_complete',
    )
    category_money = {
        'gross_sales_revenue', 'refund_amount', 'total_revenue',
        'total_ingredient_cost', 'gross_profit',
    }
    typed_categories = []
    for source in report.get('categories') or []:
        row = dict(source)
        for field in category_money | {'gross_profit_margin_pct'}:
            row[field] = _report_number(row.get(field))
        typed_categories.append(row)
    categories = _sheet_with_table(
        workbook, 'Categories', 'Category Performance', category_headers,
        typed_categories, subtitle=subtitle,
        numeric_columns=category_money | {
            'category_id', 'product_count', 'orders', 'units_sold',
            'units_refunded', 'net_units', 'gross_profit_margin_pct',
        },
    )
    category_end = 4 + len(typed_categories)
    _add_excel_table(categories, 'CategoryPerformance', 4, category_end, len(category_headers))

    daily_headers = (
        'business_date', 'orders', 'refund_events', 'units_sold',
        'units_refunded', 'net_units', 'gross_sales_revenue', 'refund_amount',
        'total_revenue', 'total_ingredient_cost', 'gross_profit',
        'gross_profit_margin_pct', 'cost_complete',
    )
    daily_money = {
        'gross_sales_revenue', 'refund_amount', 'total_revenue',
        'total_ingredient_cost', 'gross_profit',
    }
    typed_daily = []
    for source in report.get('daily') or []:
        row = dict(source)
        for field in daily_money | {'gross_profit_margin_pct'}:
            row[field] = _report_number(row.get(field))
        typed_daily.append(row)
    daily = _sheet_with_table(
        workbook, 'Daily', 'Daily Product Performance', daily_headers,
        typed_daily, subtitle=subtitle,
        numeric_columns=daily_money | {
            'orders', 'refund_events', 'units_sold', 'units_refunded',
            'net_units', 'gross_profit_margin_pct',
        },
    )
    daily_end = 4 + len(typed_daily)
    _add_excel_table(daily, 'DailyProductPerformance', 4, daily_end, len(daily_headers))

    methodology_rows = [
        ('Report status', report.get('status')),
        ('Sales event clock', (report.get('source_policy') or {}).get('sales_clock')),
        ('Refund event clock', (report.get('source_policy') or {}).get('refund_clock')),
        ('Revenue calculation', (report.get('source_policy') or {}).get('revenue')),
        ('Ingredient cost calculation', (report.get('source_policy') or {}).get('ingredient_cost')),
        ('Refund cost calculation', (report.get('source_policy') or {}).get('refund_cost')),
        ('Cost coverage policy', (report.get('coverage') or {}).get('policy')),
        ('Window convention', 'Half-open [start, end); business day 07:00 to next day 03:00 Asia/Tashkent'),
        ('Profit rule', 'Net product revenue minus historical ingredient cost after eligible return credits'),
        ('Missing cost rule', 'Profit cells remain blank until cost evidence is registered; missing cost is never treated as zero'),
    ]
    methodology = _sheet_with_table(
        workbook, 'Methodology', 'Calculation Methodology',
        ('rule', 'definition'), methodology_rows, subtitle=subtitle,
    )
    methodology.column_dimensions['A'].width = 30
    methodology.column_dimensions['B'].width = 100

    workbook.active = 0
    return _workbook_bytes(workbook)
