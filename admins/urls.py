from django.urls import path
from admins.views import auth_views, category_views, product_views, order_views
from admins.views import place_views, app_settings_views, shift_views, user_views, inkassa_views
from admins.views import (
    audit_views, export_views, dashboard_views, forecast_views,
    analytics_views, role_views, treasury_views, ai_ops_views,
)

urlpatterns = [
    # Roles & permissions editor (Settings -> Roles).
    path('permissions', role_views.list_permissions),
    path('roles', role_views.list_roles),
    path('roles/<str:name>', role_views.role_detail),

    path('auth-login', auth_views.login),
    path('auth-logout', auth_views.logout),
    path('auth-logout-all', auth_views.logout_all),
    path('auth-me', auth_views.me),
    path('auth-change-password', auth_views.change_password),
    path('auth-sessions', auth_views.sessions),

    path('categories', category_views.categories),
    path('categories/active', category_views.active_categories),
    path('categories/deleted', category_views.deleted_categories),
    path('categories/stats', category_views.category_stats),
    path('categories/reorder', category_views.reorder_categories),
    path('categories/bulk-delete', category_views.bulk_delete_categories),
    path('categories/bulk-restore', category_views.bulk_restore_categories),
    path('categories/slug/<slug:slug>', category_views.category_by_slug),
    path('categories/<int:category_id>', category_views.category_detail),
    path('categories/<int:category_id>/status', category_views.update_category_status),
    path('categories/<int:category_id>/toggle', category_views.toggle_category_status),
    path('categories/<int:category_id>/restore', category_views.restore_category),

    path('products', product_views.products),
    path('products/stats', product_views.product_stats),
    path('products/deleted', product_views.deleted_products),
    path('products/bulk-delete', product_views.bulk_delete_products),
    path('products/bulk-restore', product_views.bulk_restore_products),
    path('products/category/<int:category_id>', product_views.products_by_category),
    path('products/<int:product_id>', product_views.product_detail),
    path('products/<int:product_id>/restore', product_views.restore_product),

    path('orders', order_views.orders),
    path('orders/stats', order_views.order_stats),
    path('orders/stats/daily', order_views.daily_stats),
    path('orders/stats/monthly', order_views.monthly_stats),
    path('orders/stats/yearly', order_views.yearly_stats),
    path('orders/stats/cashiers', order_views.cashier_stats),
    path('orders/stats/statuses', order_views.status_stats),
    path('orders/stats/order-types', order_views.order_type_stats),
    path('orders/stats/top-products', order_views.top_products),
    path('orders/stats/least-sold', order_views.least_sold_products),

    path('orders/stats/categories', order_views.category_stats),
    path('orders/stats/hourly', order_views.hourly_stats),
    

    path('orders/stats/dashboard', order_views.dashboard_stats),

    path('orders/<int:order_id>', order_views.order_detail),
    path('orders/<int:order_id>/add-item', order_views.add_item),
    path('orders/<int:order_id>/status', order_views.update_status),
    path('orders/<int:order_id>/pay', order_views.pay_order),
    path('orders/<int:order_id>/unpay', order_views.unpay_order),
    path('orders/<int:order_id>/ready', order_views.mark_ready),
    path('orders/<int:order_id>/cancel', order_views.cancel_order),
    path('orders/<int:order_id>/restore', order_views.restore_order),
    path('orders/<int:order_id>/items/<int:item_id>', order_views.update_item),
    path('orders/<int:order_id>/items/<int:item_id>/remove', order_views.remove_item),
    path('orders/<int:order_id>/items/<int:item_id>/ready', order_views.mark_item_ready),
    path('orders/<int:order_id>/items/<int:item_id>/unready', order_views.unmark_item_ready),

    path('places', place_views.places),
    path('places/<int:place_id>', place_views.place_detail),
    path('tables', place_views.tables),
    path('tables/<int:table_id>', place_views.table_detail),
    path('tables/<int:table_id>/status', place_views.table_status),
    path('tables/place/<int:place_id>', place_views.tables_by_place),

    path('users', user_views.users, name='users'),
    path('users/<int:user_id>', user_views.user_detail, name='user_detail'),

    path('inkassa/balance', inkassa_views.inkassa_balance, name='inkassa_balance'),
    path('inkassa/stats', inkassa_views.inkassa_stats, name='inkassa_stats'),
    path('inkassa/history', inkassa_views.inkassa_history, name='inkassa_history'),
    path('inkassa/perform', inkassa_views.inkassa_perform, name='inkassa_perform'),
    path('inkassa/<int:inkassa_id>', inkassa_views.inkassa_detail, name='inkassa_detail'),

    # SAFE / BANK treasury: balances, transfers (with fee), expenses, ledger.
    path('treasury/accounts', treasury_views.treasury_accounts, name='treasury_accounts'),
    path('treasury/transfer', treasury_views.treasury_transfer, name='treasury_transfer'),
    path('treasury/expense', treasury_views.treasury_expense, name='treasury_expense'),
    path('treasury/history', treasury_views.treasury_history, name='treasury_history'),

    path('app-settings', app_settings_views.app_settings),

    path('shift-templates', shift_views.shift_templates),
    path('shift-templates/<int:template_id>', shift_views.shift_template_detail),
    path('shifts', shift_views.shifts),
    path('shifts/active', shift_views.active_shifts),
    path('shifts/start', shift_views.shift_start),
    path('shifts/<int:shift_id>', shift_views.shift_detail),
    path('shifts/<int:shift_id>/end', shift_views.shift_end),
    path('shifts/<int:shift_id>/reconcile', shift_views.shift_reconcile),

    path('audit-log', audit_views.audit_log, name='audit_log'),

    path('exports/1c', export_views.one_c_export, name='export_1c'),

    path('dashboard/today', dashboard_views.today_view, name='dashboard_today'),
    path('dashboard/sales', dashboard_views.sales_view, name='dashboard_sales'),
    path('dashboard/operations', dashboard_views.operations_view, name='dashboard_operations'),
    path('dashboard', dashboard_views.range_view, name='dashboard_range'),
    path('sidebar-counts', dashboard_views.sidebar_counts_view, name='sidebar_counts'),

    path('forecast/tomorrow', forecast_views.tomorrow_view, name='forecast_tomorrow'),

    path('analytics/shifts/<int:shift_id>', analytics_views.shift_perf_view, name='analytics_shift'),
    path('analytics/menu-engineering', analytics_views.menu_engineering_view, name='analytics_menu_eng'),
    # Deep shift analytics (cashier + kitchen). `<int:shift_id>` above only
    # matches integers, so these string paths don't collide with it.
    path('analytics/shifts/cashiers', analytics_views.cashier_shift_analytics_view, name='analytics_shifts_cashiers'),
    path('analytics/shifts/kitchen', analytics_views.kitchen_shift_analytics_view, name='analytics_shifts_kitchen'),
    # Shift handover report (manager view when a cashier ends their shift).
    path('analytics/shifts/<int:shift_id>/report', analytics_views.shift_report_view, name='analytics_shift_report'),

    # Products dashboard (item 9) — ?from=&to= (YYYY-MM-DD), business-day window.
    path('analytics/products/overview', analytics_views.products_overview_view, name='analytics_products_overview'),
    path('analytics/products/categories', analytics_views.products_categories_view, name='analytics_products_categories'),
    path('analytics/products/pareto', analytics_views.products_pareto_view, name='analytics_products_pareto'),
    path('analytics/products/trends', analytics_views.products_trends_view, name='analytics_products_trends'),
    path('analytics/products/affinity', analytics_views.products_affinity_view, name='analytics_products_affinity'),

    # Staff dashboard (item 10) — ?range=30d (or ?from=&to=).
    path('staff/performance', analytics_views.staff_performance_view, name='staff_performance'),

    # AI ops: Morning Briefing, context-prompt chips, Anomaly Watch (/api/admins/ai/*).
    path('ai/briefing', ai_ops_views.briefing, name='ai_briefing'),
    path('ai/briefing/dismiss', ai_ops_views.briefing_dismiss, name='ai_briefing_dismiss'),
    path('ai/context-prompts', ai_ops_views.context_prompts, name='ai_context_prompts'),
    path('ai/anomalies/settings', ai_ops_views.anomaly_settings, name='ai_anomaly_settings'),
    path('ai/anomalies/<int:anomaly_id>/ack', ai_ops_views.anomaly_ack, name='ai_anomaly_ack'),
    path('ai/anomalies', ai_ops_views.anomalies, name='ai_anomalies'),
]
