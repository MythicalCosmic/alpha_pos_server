import json
import re
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse


pytestmark = pytest.mark.django_db


def _asset_text(relative_path):
    found = finders.find(relative_path)
    assert found is not None, f'static asset not found: {relative_path}'
    return Path(found).read_text(encoding='utf-8')


def _asset_path(relative_path):
    found = finders.find(relative_path)
    assert found is not None, f'static asset not found: {relative_path}'
    return Path(found)


def test_legacy_dashboard_is_data_free_no_store_shell(client):
    response = client.get(reverse('legacy_dashboard'))

    assert response.status_code == 200
    assert response['Content-Type'].startswith('text/html')
    cache_control = response['Cache-Control']
    assert 'private' in cache_control
    assert 'no-store' in cache_control
    assert 'no-cache' in cache_control
    assert response['Pragma'] == 'no-cache'
    assert response['Expires'] == '0'

    csp = response['Content-Security-Policy']
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "form-action 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert 'unsafe-inline' not in csp
    assert 'unsafe-eval' not in csp

    body = response.content.decode()
    assert 'Trusted sales ledger' in body
    assert 'Smart Jowi Dashboard' in body
    assert 'class="admin-sidebar"' in body
    assert 'Site administration' in body
    assert 'Top 5 Products · Positive Net Units' in body
    assert 'Positive Net Revenue by Category' in body
    assert 'Non-cancelled Orders by Type' in body
    assert 'Non-cancelled Classified Orders Trend' in body
    assert 'Cashier Performance' in body
    assert 'data-range-preset="today"' in body
    assert 'data-range-preset="year"' in body
    assert '7 operating dates' in body
    assert '30 operating dates' in body
    assert '365 operating dates' in body
    assert 'Orders Created' in body
    assert 'Expected physical drawer cash now' in body
    assert 'Tender cash is not automatically the physical drawer cash' in body
    assert 'Cloud copy · sync freshness is not proven by this page' in body
    assert 'id="report-content" hidden' in body
    assert '/static/admins/legacy_dashboard.css' in body
    assert '/static/admins/vendor/chart.js' in body
    assert '/static/admins/legacy_dashboard.js' in body
    assert 'http://' not in body
    assert 'https://' not in body
    assert 'session_key=' not in body
    assert 'Set-Cookie' not in response.headers


def test_legacy_dashboard_feature_flag_returns_404(client, settings):
    settings.LEGACY_COMPAT_DASHBOARD_ENABLED = False

    response = client.get(reverse('legacy_dashboard'))

    assert response.status_code == 404


def test_legacy_dashboard_uses_existing_cookie_auth_for_protected_apis(
        client, admin_user):
    reporting_paths = (
        '/api/admins/dashboard?from=2026-07-10&to=2026-07-10',
        '/api/admins/dashboard/sales?from=2026-07-10&to=2026-07-10',
        '/api/admins/dashboard/sales/expenses'
        '?from=2026-07-10&to=2026-07-10&limit=200',
        '/api/admins/staff/performance?from=2026-07-10&to=2026-07-10',
        '/api/admins/shifts/active',
    )
    for path in reporting_paths:
        assert client.get(path).status_code == 401

    response = client.post(
        '/api/admins/auth-login',
        data=json.dumps({
            'email': admin_user.email,
            'password': 'adminpass',
        }),
        content_type='application/json',
    )
    assert response.status_code == 200
    assert response.json()['success'] is True
    assert 'session_key' in client.cookies
    assert client.cookies['session_key']['httponly'] is True
    assert client.cookies['session_key']['samesite'] == 'Lax'

    me = client.get('/api/admins/auth-me')
    assert me.status_code == 200
    assert me.json()['data']['id'] == admin_user.id
    for path in reporting_paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.content)


def test_legacy_dashboard_static_contract_is_self_contained_and_read_only():
    css = _asset_text('admins/legacy_dashboard.css')
    javascript = _asset_text('admins/legacy_dashboard.js')
    chart = _asset_text('admins/vendor/chart.js')

    for endpoint in (
        '/api/admins/auth-me',
        '/api/admins/auth-login',
        '/api/admins/auth-logout',
        '/api/admins/dashboard',
        '/api/admins/dashboard/sales',
        '/api/admins/dashboard/sales/expenses',
        '/api/admins/staff/performance',
        '/api/admins/shifts/active',
    ):
        assert endpoint in javascript

    assert 'credentials: "same-origin"' in javascript
    assert 'cache: "no-store"' in javascript
    assert 'query.set("from", dateFrom)' in javascript
    assert 'query.set("to", dateTo)' in javascript
    assert 'shift.expected_cash' in javascript
    assert 'shift.cash_to_receive_complete === true' in javascript
    assert 'shift.financial_evidence_available === true' in javascript
    assert 'Evidence incomplete' in javascript
    assert 'renderRow(item, index)' in javascript
    assert 'elements.reportContent.hidden = true' in javascript
    assert 'elements.reportContent.hidden = false' in javascript
    assert 'assertMatchingRanges(dashboard, sales, expenses, staff)' in javascript
    assert 'No figures were displayed' in javascript
    assert 'evidence.attribution_complete === true' in javascript
    assert 'evidence.unknown_sales' in javascript
    assert 'evidence.unknown_refunds' in javascript
    assert 'The previous value was cleared' in javascript
    assert 'Date selection changed. Apply the range' in javascript
    assert 'data-range-preset' in javascript
    assert 'addDays(today, -29)' in javascript
    assert 'addDays(today, -364)' in javascript
    assert 'window.Chart' in javascript
    assert 'renderCharts(dashboard, sales)' in javascript
    assert javascript.index(
        'renderCharts(dashboard, sales);'
    ) < javascript.index(
        'elements.reportContent.hidden = false;',
        javascript.index('renderCharts(dashboard, sales);'),
    )
    assert 'const netRevenue = numeric(dashboard.revenue)' in javascript
    assert 'formatMoney(netRevenue / paidOrders)' in javascript
    assert 'document.createElement("div")' in javascript
    assert 'innerHTML' not in javascript

    assert '@font-face' in css
    assert 'fonts/PlusJakartaSans-Variable.ttf' in css
    assert 'fonts/Material-Symbols-Outlined.woff2' in css
    assert 'grid-template-columns: repeat(4' in css
    assert 'grid-template-columns: repeat(5' in css
    assert 'version="4.4.0"' in chart

    assert _asset_path(
        'admins/fonts/PlusJakartaSans-Variable.ttf'
    ).stat().st_size > 100_000
    assert _asset_path(
        'admins/fonts/Material-Symbols-Outlined.woff2'
    ).stat().st_size > 100_000
    assert 'SIL OPEN FONT LICENSE' in _asset_text(
        'admins/fonts/OFL-PlusJakartaSans.txt'
    )
    assert 'Apache License' in _asset_text(
        'admins/fonts/LICENSE-Material-Symbols.txt'
    )
    assert 'The MIT License' in _asset_text(
        'admins/vendor/LICENSE-Chart.js.md'
    )
    notices = _asset_text('admins/THIRD_PARTY_NOTICES.txt')
    assert 'Plus Jakarta Sans' in notices
    assert 'Material Symbols Outlined' in notices
    assert 'Chart.js 4.4.0' in notices

    forbidden_client_storage = (
        'localStorage',
        'sessionStorage',
        'document.cookie',
        'Authorization',
        'Bearer ',
        '.token',
    )
    for fragment in forbidden_client_storage:
        assert fragment not in javascript

    assert re.search(r'https?://', css) is None
    assert re.search(r'https?://', javascript) is None

    view_source = (
        Path(__file__).parents[2] / 'views' / 'legacy_dashboard_views.py'
    ).read_text(encoding='utf-8')
    assert 'admins.services' not in view_source
    assert '.objects' not in view_source


def test_legacy_dashboard_source_contract_states_event_grains_and_chart_scope():
    template = (
        Path(__file__).parents[2]
        / 'templates' / 'admins' / 'legacy_dashboard.html'
    ).read_text(encoding='utf-8')
    javascript = _asset_text('admins/legacy_dashboard.js')

    for label in (
        'Orders Created',
        'Top 5 Products · Positive Net Units',
        'Positive Net Revenue by Category',
        'Non-cancelled Orders by Type',
        'Non-cancelled Classified Orders Trend',
        'created_at · HALL / DELIVERY / PICKUP',
        '7 operating dates',
        '30 operating dates',
        '365 operating dates',
    ):
        assert label in template

    assert 'paid settlements (paid_at)' in javascript
    assert 'cancellations (created_at)' in javascript
    assert 'Non-cancelled classified orders (created_at)' in javascript

    product_block_start = javascript.index('const chartProducts = products.filter')
    product_block_end = javascript.index(
        'const categories = Array.isArray', product_block_start,
    )
    product_block = javascript[product_block_start:product_block_end]
    assert '(numeric(product.quantity) ?? 0) > 0' in product_block
    assert '(numeric(product.revenue) ?? 0) > 0' in product_block
    assert 'shownProductUnits' in product_block
    assert 'dashboard.units_sold' not in product_block
    assert 'non-positive or ' in product_block
    assert 'refund-heavy rows were omitted' in product_block

    category_block_start = javascript.index(
        'const positiveCategories = categories.filter',
    )
    category_block_end = javascript.index(
        'const channelDays = Array.isArray', category_block_start,
    )
    category_block = javascript[category_block_start:category_block_end]
    assert '(numeric(category.revenue) ?? 0) > 0' in category_block
    assert 'zero or negative refund-heavy rows were ' in category_block

    login_start = javascript.index('async function handleLogin')
    login_end = javascript.index('async function handleLogout', login_start)
    login_block = javascript[login_start:login_end]
    captured_at = login_block.index(
        'const password = elements.loginPassword.value;',
    )
    cleared_at = login_block.index('elements.loginPassword.value = "";')
    requested_at = login_block.index('await apiRequest(API.login')
    assert captured_at < cleared_at < requested_at

    change_start = javascript.index(
        '[elements.dateFrom, elements.dateTo].forEach',
    )
    change_end = javascript.index(
        'elements.presetButtons.forEach', change_start,
    )
    change_block = javascript[change_start:change_end]
    assert 'currentRequest += 1;' in change_block
    assert 'elements.applyRange.disabled = false;' in change_block
    assert change_block.index('currentRequest += 1;') < change_block.index(
        'elements.reportContent.hidden = true;',
    )
    assert 'if (requestId !== currentRequest)' in javascript
