"""File renderers for the canonical product performance report."""

import csv
from io import BytesIO, StringIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings

from admins.services.workbook_export_service import (
    XLSX_CONTENT_TYPE,
    build_product_performance_workbook,
)


PDF_CONTENT_TYPE = 'application/pdf'
CSV_CONTENT_TYPE = 'text/csv; charset=utf-8'
EXPORT_FORMATS = {
    'xlsx': (XLSX_CONTENT_TYPE, 'xlsx'),
    'pdf': (PDF_CONTENT_TYPE, 'pdf'),
    'csv': (CSV_CONTENT_TYPE, 'csv'),
}
_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@')


def _csv_value(value, *, numeric=False):
    if value is None:
        return ''
    text = str(value).replace('\x00', '')
    if not numeric and text.lstrip().startswith(_CSV_FORMULA_PREFIXES):
        text = "'" + text
    return text


def build_product_performance_csv(report):
    """Return one UTF-8 CSV with filters, the full product table, and totals."""
    stream = StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    date_range = report.get('range') or {}
    filters = report.get('filters') or {}
    summary = report.get('summary') or {}
    writer.writerow(['Alpha POS Product Performance Report'])
    writer.writerow(['Business date from', _csv_value(date_range.get('from'))])
    writer.writerow(['Business date to', _csv_value(date_range.get('to'))])
    writer.writerow(['Preset', _csv_value(date_range.get('preset'))])
    writer.writerow(['Branch', _csv_value(report.get('branch_id'))])
    writer.writerow(['Currency', _csv_value(report.get('currency'))])
    writer.writerow(['Sort', _csv_value(filters.get('sort'))])
    writer.writerow(['Category ID', _csv_value(filters.get('category_id'))])
    writer.writerow(['Search', _csv_value(filters.get('search'))])
    writer.writerow(['Generated at', _csv_value(report.get('generated_at'))])
    writer.writerow([])

    headers = (
        'Rank', 'Product ID', 'Product name', 'Category', 'Units sold',
        'Units refunded', 'Net units', 'Orders sold',
        'Average selling price per unit (UZS)', 'Gross sales revenue (UZS)',
        'Refund amount (UZS)', 'Net total revenue (UZS)',
        'Ingredient cost per unit (UZS)', 'Total ingredient cost (UZS)',
        'Gross profit per item (UZS)', 'Gross profit (UZS)',
        'Gross profit margin (%)', 'Cost source', 'Cost coverage (%)',
        'Cost complete', 'Current catalog price (UZS)',
    )
    keys = (
        'rank', 'product_id', 'product_name', 'category_name', 'units_sold',
        'units_refunded', 'net_units', 'orders_sold',
        'selling_price_per_unit', 'gross_sales_revenue', 'refund_amount',
        'total_revenue', 'ingredient_cost_per_unit', 'total_ingredient_cost',
        'gross_profit_per_item', 'gross_profit', 'gross_profit_margin_pct',
        'cost_source', 'cost_coverage_pct', 'cost_complete',
        'current_catalog_price',
    )
    numeric_keys = {
        key for key in keys
        if key not in {
            'product_name', 'category_name', 'cost_source', 'cost_complete',
        }
    }
    writer.writerow(headers)
    for row in report.get('products') or []:
        writer.writerow([
            _csv_value(row.get(key), numeric=key in numeric_keys) for key in keys
        ])
    total = [''] * len(headers)
    total[0] = 'TOTAL'
    total[4] = summary.get('total_units_sold')
    total[5] = summary.get('total_units_refunded')
    total[6] = summary.get('net_units')
    total[9] = summary.get('gross_sales_revenue')
    total[10] = summary.get('refund_amount')
    total[11] = summary.get('total_revenue')
    total[13] = summary.get('total_ingredient_cost')
    total[15] = summary.get('total_gross_profit')
    total[16] = summary.get('gross_profit_margin_pct')
    total[18] = summary.get('cost_coverage_pct')
    total[19] = summary.get('cost_complete')
    writer.writerow([
        _csv_value(value, numeric=index not in {0, 17, 19})
        for index, value in enumerate(total)
    ])
    writer.writerow([])
    writer.writerow(['Cost policy', _csv_value((report.get('coverage') or {}).get('policy'))])
    writer.writerow([
        'Missing cost rule',
        'Profit fields are blank when historical cost evidence is incomplete.',
    ])
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


def _pdf_number(value, *, suffix=''):
    if value in (None, ''):
        return 'Incomplete'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    rendered = f'{number:,.2f}'
    if rendered.endswith('.00'):
        rendered = rendered[:-3]
    return f'{rendered}{suffix}'


def _pdf_text(value, limit=80):
    text = str(value if value not in (None, '') else '—')
    if len(text) > limit:
        text = text[:limit - 1] + '…'
    return escape(text)


def _register_pdf_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    name = 'AlphaPOSOpenSans'
    if name not in pdfmetrics.getRegisteredFontNames():
        path = (
            Path(settings.BASE_DIR) / 'admins' / 'static' / 'admins' / 'fonts'
            / 'OpenSans-Regular.ttf'
        )
        pdfmetrics.registerFont(TTFont(name, str(path)))
    return name


def build_product_performance_pdf(report):
    """Return a multi-page, Unicode PDF containing every filtered product."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
        TableStyle,
    )

    font_name = _register_pdf_font()
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=13 * mm,
        bottomMargin=13 * mm,
        title='Alpha POS Product Performance Report',
        author='Alpha POS',
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'ReportTitle', parent=styles['Title'], fontName=font_name,
        fontSize=20, leading=24, textColor=colors.HexColor('#17324D'),
        alignment=TA_LEFT, spaceAfter=5,
    )
    subtitle_style = ParagraphStyle(
        'ReportSubtitle', parent=styles['Normal'], fontName=font_name,
        fontSize=8.5, leading=11, textColor=colors.HexColor('#526575'),
        spaceAfter=10,
    )
    section_style = ParagraphStyle(
        'ReportSection', parent=styles['Heading2'], fontName=font_name,
        fontSize=13, leading=16, textColor=colors.HexColor('#17324D'),
        spaceBefore=8, spaceAfter=6,
    )
    cell_style = ParagraphStyle(
        'ReportCell', parent=styles['Normal'], fontName=font_name,
        fontSize=6.4, leading=8, alignment=TA_LEFT,
    )
    number_style = ParagraphStyle(
        'ReportNumber', parent=cell_style, alignment=TA_RIGHT,
    )
    header_style = ParagraphStyle(
        'ReportHeader', parent=cell_style, textColor=colors.white,
        alignment=TA_CENTER, fontSize=6.1, leading=7.2,
    )
    note_style = ParagraphStyle(
        'ReportNote', parent=styles['Normal'], fontName=font_name,
        fontSize=7, leading=9, textColor=colors.HexColor('#526575'),
    )

    def paragraph(value, style=cell_style, limit=80):
        return Paragraph(_pdf_text(value, limit), style)

    story = []
    date_range = report.get('range') or {}
    filters = report.get('filters') or {}
    summary = report.get('summary') or {}
    story.append(Paragraph('Alpha POS · Product Performance', title_style))
    story.append(Paragraph(
        _pdf_text(
            f'Business dates {date_range.get("from")} to {date_range.get("to")} · '
            f'Branch {report.get("branch_id")} · UZS · Generated {report.get("generated_at")}',
            300,
        ), subtitle_style,
    ))

    card_data = [
        ['Products', 'Units sold', 'Net revenue', 'Ingredient cost', 'Gross profit', 'Gross margin'],
        [
            _pdf_number(summary.get('product_count')),
            _pdf_number(summary.get('total_units_sold')),
            _pdf_number(summary.get('total_revenue')),
            _pdf_number(summary.get('total_ingredient_cost')),
            _pdf_number(summary.get('total_gross_profit')),
            _pdf_number(summary.get('gross_profit_margin_pct'), suffix='%'),
        ],
    ]
    cards = Table(card_data, colWidths=[43 * mm] * 6, rowHeights=[8 * mm, 11 * mm])
    cards.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E8F3F4')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#355465')),
        ('TEXTCOLOR', (0, 1), (-1, 1), colors.HexColor('#17324D')),
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, 0), 7),
        ('FONTSIZE', (0, 1), (-1, 1), 11),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#C8D7DF')),
        ('FONTNAME', (0, 1), (-1, 1), font_name),
    ]))
    story.append(cards)
    story.append(Spacer(1, 6 * mm))
    applied = ', '.join(
        f'{key}={value}' for key, value in filters.items()
        if value not in (None, '')
    ) or 'No additional filters'
    story.append(Paragraph(_pdf_text(f'Filters: {applied}', 400), note_style))
    if not summary.get('cost_complete', True):
        story.append(Paragraph(
            _pdf_text(
                'Cost coverage is incomplete. Profit values stay blank for affected '
                'products; missing cost is never counted as zero.', 400,
            ), note_style,
        ))

    story.append(Paragraph('Products', section_style))
    headers = (
        '#', 'Product', 'Category', 'Units', 'Avg sale price', 'Net revenue',
        'Ingredient / unit', 'Ingredient total', 'Profit / item', 'Gross profit',
        'Margin', 'Cost evidence',
    )
    product_data = [[paragraph(header, header_style, 30) for header in headers]]
    for row in report.get('products') or []:
        product_data.append([
            paragraph(row.get('rank'), number_style),
            paragraph(row.get('product_name'), cell_style, 36),
            paragraph(row.get('category_name'), cell_style, 28),
            paragraph(_pdf_number(row.get('units_sold')), number_style),
            paragraph(_pdf_number(row.get('selling_price_per_unit')), number_style),
            paragraph(_pdf_number(row.get('total_revenue')), number_style),
            paragraph(_pdf_number(row.get('ingredient_cost_per_unit')), number_style),
            paragraph(_pdf_number(row.get('total_ingredient_cost')), number_style),
            paragraph(_pdf_number(row.get('gross_profit_per_item')), number_style),
            paragraph(_pdf_number(row.get('gross_profit')), number_style),
            paragraph(_pdf_number(row.get('gross_profit_margin_pct'), suffix='%'), number_style),
            paragraph(row.get('cost_source'), cell_style, 22),
        ])
    product_data.append([
        paragraph('TOTAL', header_style), '', '',
        paragraph(_pdf_number(summary.get('total_units_sold')), header_style), '',
        paragraph(_pdf_number(summary.get('total_revenue')), header_style), '',
        paragraph(_pdf_number(summary.get('total_ingredient_cost')), header_style), '',
        paragraph(_pdf_number(summary.get('total_gross_profit')), header_style),
        paragraph(_pdf_number(summary.get('gross_profit_margin_pct'), suffix='%'), header_style),
        paragraph(f'Coverage {summary.get("cost_coverage_pct") or "—"}%', header_style),
    ])
    product_table = Table(
        product_data,
        repeatRows=1,
        colWidths=[13*mm, 31*mm, 25*mm, 12*mm, 21*mm, 22*mm, 22*mm,
                   22*mm, 20*mm, 21*mm, 15*mm, 31*mm],
        splitByRow=1,
    )
    product_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#18A7A0')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#17324D')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('TEXTCOLOR', (0, -1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#D9E2E8')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#F6FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(product_table)

    story.append(PageBreak())
    story.append(Paragraph('Daily performance', section_style))
    daily_headers = ('Business date', 'Orders', 'Units', 'Revenue', 'Cost', 'Profit', 'Margin')
    daily_data = [[paragraph(header, header_style) for header in daily_headers]]
    for row in report.get('daily') or []:
        daily_data.append([
            paragraph(row.get('business_date')),
            paragraph(row.get('orders'), number_style),
            paragraph(row.get('units_sold'), number_style),
            paragraph(_pdf_number(row.get('total_revenue')), number_style),
            paragraph(_pdf_number(row.get('total_ingredient_cost')), number_style),
            paragraph(_pdf_number(row.get('gross_profit')), number_style),
            paragraph(_pdf_number(row.get('gross_profit_margin_pct'), suffix='%'), number_style),
        ])
    daily_table = Table(
        daily_data, repeatRows=1,
        colWidths=[38*mm, 28*mm, 28*mm, 38*mm, 38*mm, 38*mm, 30*mm],
    )
    daily_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#18A7A0')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#D9E2E8')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F6FAFC')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(daily_table)
    story.append(Spacer(1, 7 * mm))
    story.append(Paragraph('Category performance', section_style))
    category_headers = ('Category', 'Products', 'Units', 'Revenue', 'Cost', 'Profit', 'Margin')
    category_data = [[paragraph(header, header_style) for header in category_headers]]
    for row in report.get('categories') or []:
        category_data.append([
            paragraph(row.get('category_name')),
            paragraph(row.get('product_count'), number_style),
            paragraph(row.get('units_sold'), number_style),
            paragraph(_pdf_number(row.get('total_revenue')), number_style),
            paragraph(_pdf_number(row.get('total_ingredient_cost')), number_style),
            paragraph(_pdf_number(row.get('gross_profit')), number_style),
            paragraph(_pdf_number(row.get('gross_profit_margin_pct'), suffix='%'), number_style),
        ])
    category_table = Table(
        category_data, repeatRows=1,
        colWidths=[55*mm, 27*mm, 28*mm, 38*mm, 38*mm, 38*mm, 30*mm],
    )
    category_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#18A7A0')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#D9E2E8')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F6FAFC')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(category_table)
    story.append(Spacer(1, 8 * mm))
    story.append(KeepTogether([
        Paragraph('Methodology', section_style),
        Paragraph(_pdf_text((report.get('coverage') or {}).get('policy'), 800), note_style),
        Paragraph(_pdf_text((report.get('source_policy') or {}).get('refund_cost'), 800), note_style),
    ]))

    def decorate_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor('#647786'))
        canvas.drawString(10 * mm, 7 * mm, 'Alpha POS · Product Performance')
        canvas.drawRightString(
            landscape(A4)[0] - 10 * mm,
            7 * mm,
            f'Page {doc.page}',
        )
        canvas.restoreState()

    document.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
    return output.getvalue()


def render_product_performance_export(report, file_format):
    if file_format == 'xlsx':
        return build_product_performance_workbook(report)
    if file_format == 'pdf':
        return build_product_performance_pdf(report)
    if file_format == 'csv':
        return build_product_performance_csv(report)
    raise ValueError(f'Unsupported export format: {file_format}')
