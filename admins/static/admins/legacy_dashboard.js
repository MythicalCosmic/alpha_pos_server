(() => {
    "use strict";

    const API = Object.freeze({
        me: "/api/admins/auth-me",
        login: "/api/admins/auth-login",
        logout: "/api/admins/auth-logout",
        dashboard: "/api/admins/dashboard",
        sales: "/api/admins/dashboard/sales",
        expenses: "/api/admins/dashboard/sales/expenses",
        staff: "/api/admins/staff/performance",
        activeShifts: "/api/admins/shifts/active",
    });

    const moneyFormatter = new Intl.NumberFormat("uz-UZ", {
        maximumFractionDigits: 0,
    });
    const dateTimeFormatter = new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Tashkent",
        dateStyle: "medium",
        timeStyle: "short",
    });
    const headerTimeFormatter = new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Tashkent",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
    });
    const CHART_COLORS = Object.freeze([
        "#6366f1",
        "#10b981",
        "#f59e0b",
        "#f43f5e",
        "#8b5cf6",
        "#06b6d4",
        "#3b82f6",
        "#ec4899",
    ]);

    class AuthenticationRequired extends Error {}

    const elements = {};
    const charts = new Map();
    let currentRequest = 0;

    function element(id) {
        return document.getElementById(id);
    }

    function setText(id, value) {
        const node = element(id);
        if (node) {
            node.textContent = value === null || value === undefined || value === ""
                ? "—"
                : String(value);
        }
    }

    function numeric(value) {
        if (value === null || value === undefined || value === "") {
            return null;
        }
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function formatMoney(value) {
        const parsed = numeric(value);
        return parsed === null ? "—" : `${moneyFormatter.format(parsed)} UZS`;
    }

    function formatCount(value) {
        const parsed = numeric(value);
        return parsed === null ? "—" : moneyFormatter.format(parsed);
    }

    function formatCompactNumber(value) {
        const parsed = numeric(value);
        if (parsed === null) {
            return "—";
        }
        const absolute = Math.abs(parsed);
        if (absolute >= 1000000) {
            return `${(parsed / 1000000).toFixed(1)}M`;
        }
        if (absolute >= 1000) {
            return `${(parsed / 1000).toFixed(0)}K`;
        }
        return moneyFormatter.format(parsed);
    }

    function formatDateTime(value) {
        if (!value) {
            return "—";
        }
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? String(value) : dateTimeFormatter.format(parsed);
    }

    function humanize(value) {
        if (!value) {
            return "Unavailable";
        }
        return String(value)
            .toLowerCase()
            .split("_")
            .filter(Boolean)
            .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
            .join(" ");
    }

    async function apiRequest(url, options = {}) {
        const headers = new Headers(options.headers || {});
        headers.set("Accept", "application/json");
        if (options.body) {
            headers.set("Content-Type", "application/json");
        }
        const response = await fetch(url, {
            ...options,
            headers,
            credentials: "same-origin",
            cache: "no-store",
        });
        let payload = null;
        try {
            payload = await response.json();
        } catch (_error) {
            payload = null;
        }
        if (response.status === 401 || response.status === 403) {
            throw new AuthenticationRequired(
                payload && payload.message ? payload.message : "Authentication required",
            );
        }
        if (!response.ok || !payload || payload.success !== true) {
            throw new Error(
                payload && payload.message
                    ? payload.message
                    : `Request failed (${response.status})`,
            );
        }
        return payload.data;
    }

    function endpointWithRange(path, dateFrom, dateTo, extra = {}) {
        const query = new URLSearchParams();
        query.set("from", dateFrom);
        query.set("to", dateTo);
        Object.entries(extra).forEach(([key, value]) => query.set(key, String(value)));
        return `${path}?${query.toString()}`;
    }

    function tashkentBusinessDate() {
        const now = new Date();
        const parts = new Intl.DateTimeFormat("en-US", {
            timeZone: "Asia/Tashkent",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            hourCycle: "h23",
        }).formatToParts(now);
        const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
        const year = Number(values.year);
        const month = Number(values.month);
        const day = Number(values.day);
        const hour = Number(values.hour);
        const date = new Date(Date.UTC(year, month - 1, day));
        if (hour < 3) {
            date.setUTCDate(date.getUTCDate() - 1);
        }
        return [
            date.getUTCFullYear(),
            String(date.getUTCMonth() + 1).padStart(2, "0"),
            String(date.getUTCDate()).padStart(2, "0"),
        ].join("-");
    }

    function parseISODate(value) {
        const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
        if (!match) {
            return null;
        }
        return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
    }

    function formatISODate(date) {
        return [
            date.getUTCFullYear(),
            String(date.getUTCMonth() + 1).padStart(2, "0"),
            String(date.getUTCDate()).padStart(2, "0"),
        ].join("-");
    }

    function addDays(value, days) {
        const date = parseISODate(value);
        if (!date) {
            return value;
        }
        date.setUTCDate(date.getUTCDate() + days);
        return formatISODate(date);
    }

    function presetRange(preset) {
        const today = tashkentBusinessDate();
        if (preset === "yesterday") {
            const yesterday = addDays(today, -1);
            return [yesterday, yesterday];
        }
        if (preset === "week") {
            return [addDays(today, -6), today];
        }
        if (preset === "month") {
            return [addDays(today, -29), today];
        }
        if (preset === "year") {
            return [addDays(today, -364), today];
        }
        return [today, today];
    }

    function setActivePreset(activePreset = "") {
        elements.presetButtons.forEach((button) => {
            const active = button.dataset.rangePreset === activePreset;
            button.classList.toggle("active", active);
            button.setAttribute("aria-pressed", active ? "true" : "false");
        });
    }

    function syncPresetState() {
        const match = ["today", "yesterday", "week", "month", "year"].find((preset) => {
            const [dateFrom, dateTo] = presetRange(preset);
            return elements.dateFrom.value === dateFrom && elements.dateTo.value === dateTo;
        });
        setActivePreset(match || "");
    }

    function updateHeaderTime() {
        setText("dashboard-current-time", headerTimeFormatter.format(new Date()).replace(",", ""));
    }

    function setMessage(message, kind = "") {
        elements.pageMessage.textContent = message || "";
        elements.pageMessage.className = `page-message${kind ? ` ${kind}` : ""}`;
    }

    function showLogin(message = "") {
        elements.dashboardView.hidden = true;
        elements.loginView.hidden = false;
        elements.loginError.textContent = message;
        elements.loginError.hidden = !message;
        elements.loginPassword.value = "";
        elements.loginEmail.focus();
    }

    function showDashboard(user) {
        elements.loginView.hidden = true;
        elements.dashboardView.hidden = false;
        const fullName = [user.first_name, user.last_name].filter(Boolean).join(" ");
        setText("account-name", fullName || user.email);
    }

    function appendCell(row, value, className = "") {
        const cell = document.createElement("td");
        cell.textContent = value === null || value === undefined || value === ""
            ? "—"
            : String(value);
        if (className) {
            cell.className = className;
        }
        row.appendChild(cell);
        return cell;
    }

    function appendBadge(cell, label, kind) {
        const badge = document.createElement("span");
        badge.className = `badge ${kind}`;
        badge.textContent = label;
        cell.replaceChildren(badge);
    }

    function replaceRows(body, rows, renderRow) {
        const fragment = document.createDocumentFragment();
        rows.forEach((item, index) => fragment.appendChild(renderRow(item, index)));
        body.replaceChildren(fragment);
    }

    function replaceDetails(container, rows) {
        const fragment = document.createDocumentFragment();
        rows.forEach(([label, value]) => {
            const row = document.createElement("div");
            const term = document.createElement("dt");
            const detail = document.createElement("dd");
            term.textContent = label;
            detail.textContent = value;
            row.append(term, detail);
            fragment.appendChild(row);
        });
        container.replaceChildren(fragment);
    }

    function replaceDataList(container, rows, emptyMessage) {
        const fragment = document.createDocumentFragment();
        rows.forEach(({ title, subtitle, value, tone }) => {
            const row = document.createElement("div");
            const main = document.createElement("div");
            const titleNode = document.createElement("span");
            const subtitleNode = document.createElement("span");
            const valueNode = document.createElement("span");
            row.className = "data-row";
            main.className = "data-main";
            titleNode.className = "data-title";
            subtitleNode.className = "data-subtitle";
            valueNode.className = `data-value${tone ? ` ${tone}` : ""}`;
            titleNode.textContent = title || "—";
            subtitleNode.textContent = subtitle || "";
            valueNode.textContent = value || "—";
            main.append(titleNode, subtitleNode);
            row.append(main, valueNode);
            fragment.appendChild(row);
        });
        if (!rows.length) {
            const empty = document.createElement("p");
            empty.className = "empty-state";
            empty.textContent = emptyMessage;
            fragment.appendChild(empty);
        }
        container.replaceChildren(fragment);
    }

    function replaceLegend(container, rows) {
        const fragment = document.createDocumentFragment();
        rows.forEach(({ label, value, color }) => {
            const row = document.createElement("div");
            const colorNode = document.createElement("span");
            const labelNode = document.createElement("span");
            const valueNode = document.createElement("span");
            row.className = "legend-item";
            colorNode.className = "legend-color";
            colorNode.style.backgroundColor = color;
            labelNode.className = "legend-label";
            labelNode.textContent = label;
            valueNode.className = "legend-value";
            valueNode.textContent = value;
            row.append(colorNode, labelNode, valueNode);
            fragment.appendChild(row);
        });
        container.replaceChildren(fragment);
    }

    function destroyChart(id) {
        const existing = charts.get(id);
        if (existing) {
            existing.destroy();
            charts.delete(id);
        }
    }

    function createChart(id, config) {
        destroyChart(id);
        const canvas = element(id);
        if (!canvas || !window.Chart) {
            return null;
        }
        const chart = new window.Chart(canvas, config);
        charts.set(id, chart);
        return chart;
    }

    function numericSeries(values) {
        return Array.isArray(values)
            ? values.map((value) => numeric(value) ?? 0)
            : [];
    }

    function lineChartOptions({ money = false, legend = false } = {}) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: "index" },
            plugins: {
                legend: {
                    display: legend,
                    position: "top",
                    labels: { usePointStyle: true, padding: 14 },
                },
                tooltip: {
                    backgroundColor: "rgba(15, 23, 42, 0.96)",
                    padding: 12,
                    cornerRadius: 10,
                    callbacks: money
                        ? { label: (context) => `${context.dataset.label}: ${formatMoney(context.parsed.y)}` }
                        : {},
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: "rgba(255, 255, 255, 0.035)" },
                    ticks: money ? { callback: (value) => formatCompactNumber(value) } : {},
                },
                x: {
                    grid: { display: false },
                    ticks: { maxRotation: 45, minRotation: 0 },
                },
            },
        };
    }

    function assertMatchingRanges(dashboard, sales, expenses, staff) {
        const expected = dashboard && dashboard.range;
        const ranges = [sales && sales.range, expenses && expenses.range, staff && staff.range];
        if (
            !expected
            || !expected.start_at
            || !expected.end_at
            || !ranges.every(
                (item) => (
                    item
                    && item.start_at === expected.start_at
                    && item.end_at === expected.end_at
                ),
            )
        ) {
            throw new Error(
                "Reporting endpoints returned different or incomplete counted intervals. No figures were displayed.",
            );
        }
    }

    function renderRange(dashboard, sales, expenses, staff) {
        const range = dashboard.range || {};
        setText("effective-start", formatDateTime(range.start_at));
        setText("effective-end", formatDateTime(range.end_at));
        setText("range-mode", humanize(range.mode));
        setText("range-timezone", range.timezone);
        setText(
            "period-label",
            range.from && range.to
                ? (range.from === range.to ? range.from : `${range.from} → ${range.to}`)
                : "Selected operating period",
        );

        elements.rangeConsistency.textContent =
            "All selected-range endpoints resolved to the same effective interval.";
        elements.rangeConsistency.classList.remove("problem");
    }

    function renderSummary(dashboard, expenses) {
        const payments = dashboard.payment_breakdown || {};
        const evidence = dashboard.tender_evidence || {};
        setText("kpi-net", formatMoney(dashboard.revenue));
        setText("kpi-gross", formatMoney(dashboard.gross_revenue));
        setText("kpi-refunds", formatMoney(dashboard.refund_amount));
        setText("kpi-refunded-orders", `${formatCount(dashboard.refunded_orders)} refund events`);
        setText("kpi-cash", formatMoney(payments.cash));
        setText("kpi-card", formatMoney(payments.card));
        setText("kpi-payme", formatMoney(payments.payme));
        setText("kpi-expenses", formatMoney(expenses.total_expense));
        setText("kpi-orders", formatCount(dashboard.orders));
        setText(
            "kpi-order-detail",
            `${formatCount(dashboard.paid_orders)} paid settlements (paid_at) · `
            + `${formatCount(dashboard.cancelled)} cancellations (created_at)`,
        );
        setText("kpi-units", formatCount(dashboard.units_sold));
        const netRevenue = numeric(dashboard.revenue);
        const paidOrders = numeric(dashboard.paid_orders);
        setText(
            "kpi-average",
            netRevenue !== null && paidOrders !== null && paidOrders > 0
                ? formatMoney(netRevenue / paidOrders)
                : "—",
        );

        const previousRevenue = numeric(
            dashboard.previous_period && dashboard.previous_period.revenue,
        );
        const currentRevenue = numeric(dashboard.revenue);
        const growthNode = element("kpi-net-growth");
        growthNode.classList.remove("negative");
        if (previousRevenue === null || currentRevenue === null) {
            growthNode.textContent = "—";
        } else if (previousRevenue === 0) {
            growthNode.textContent = currentRevenue === 0 ? "0.0%" : "New";
        } else {
            const growth = ((currentRevenue - previousRevenue) / Math.abs(previousRevenue)) * 100;
            growthNode.textContent = `${growth >= 0 ? "↗" : "↘"} ${growth.toFixed(1)}%`;
            growthNode.classList.toggle("negative", growth < 0);
        }

        const attributionComplete = evidence.attribution_complete === true;
        elements.unknownWarning.hidden = attributionComplete;
        if (!elements.unknownWarning.hidden) {
            setText(
                "unknown-amount",
                `Unattributed sales: ${formatMoney(evidence.unknown_sales)} · `
                + `unattributed refunds: ${formatMoney(evidence.unknown_refunds)} · `
                + `net: ${formatMoney(payments.unknown || "0")}`,
            );
        }
    }

    function renderTender(dashboard) {
        const payments = dashboard.payment_breakdown || {};
        const evidence = dashboard.tender_evidence || {};
        const cardDetail = payments.card_detail || {};
        const rows = [
            ["Cash tender · selected range", formatMoney(payments.cash)],
            ["All cards", formatMoney(payments.card)],
            ...Object.entries(cardDetail).map(
                ([method, amount]) => [`↳ ${method}`, formatMoney(amount)],
            ),
            ["Payme", formatMoney(payments.payme)],
            ["Unknown / unattributed", formatMoney(payments.unknown || "0")],
            ["Unattributed sales evidence", formatMoney(evidence.unknown_sales)],
            ["Unattributed refund evidence", formatMoney(evidence.unknown_refunds)],
        ];
        replaceDetails(elements.tenderDetail, rows);
    }

    function renderPrevious(dashboard) {
        const previous = dashboard.previous_period || {};
        const range = previous.range || {};
        replaceDetails(elements.previousDetail, [
            ["Operating dates", range.from && range.to ? `${range.from} → ${range.to}` : "—"],
            ["Net sales", formatMoney(previous.revenue)],
            ["Gross sales", formatMoney(previous.gross_revenue)],
            ["Refunds", formatMoney(previous.refund_amount)],
            ["Orders created", formatCount(previous.orders)],
            ["Paid orders", formatCount(previous.paid_orders)],
            ["Cancelled orders", formatCount(previous.cancelled)],
            ["Net units", formatCount(previous.units_sold)],
        ]);
    }

    function renderDailySales(sales) {
        const labels = Array.isArray(sales.dayLabels) ? sales.dayLabels : [];
        const gross = Array.isArray(sales.grossRevenue30) ? sales.grossRevenue30 : [];
        const refunds = Array.isArray(sales.refund30) ? sales.refund30 : [];
        const net = Array.isArray(sales.revenue30) ? sales.revenue30 : [];
        const expenses = Array.isArray(sales.expense30) ? sales.expense30 : [];
        replaceRows(elements.dailySalesBody, labels, (label, index) => {
            const row = document.createElement("tr");
            appendCell(row, label);
            appendCell(row, formatMoney(gross[index]), "numeric");
            appendCell(row, formatMoney(refunds[index]), "numeric");
            appendCell(row, formatMoney(net[index]), "numeric");
            appendCell(row, formatMoney(expenses[index]), "numeric");
            return row;
        });
    }

    function renderProducts(dashboard) {
        const products = Array.isArray(dashboard.top_products) ? dashboard.top_products : [];
        setText("product-summary", `${formatCount(dashboard.units_sold)} net units`);
        replaceDataList(
            elements.productList,
            products.map((product) => ({
                title: product.product_name || "Unnamed product",
                subtitle: `${formatCount(product.quantity)} net units sold`,
                value: formatMoney(product.revenue),
                tone: "emerald",
            })),
            "No settled products in this interval.",
        );
        replaceRows(elements.productsBody, products, (product) => {
            const row = document.createElement("tr");
            appendCell(row, product.product_name || "Unnamed product");
            appendCell(row, formatCount(product.quantity), "numeric");
            appendCell(row, formatMoney(product.revenue), "numeric");
            return row;
        });
        if (!products.length) {
            const row = document.createElement("tr");
            const cell = appendCell(row, "No settled product lines in this interval.");
            cell.colSpan = 3;
            elements.productsBody.replaceChildren(row);
        }
    }

    function renderCategories(dashboard) {
        const categories = Array.isArray(dashboard.category_stats)
            ? dashboard.category_stats
            : [];
        replaceRows(elements.categoriesBody, categories, (category) => {
            const row = document.createElement("tr");
            appendCell(row, category.category || "Uncategorized");
            appendCell(row, formatCount(category.quantity), "numeric");
            appendCell(row, formatMoney(category.revenue), "numeric");
            return row;
        });
        if (!categories.length) {
            const row = document.createElement("tr");
            const cell = appendCell(row, "No settled category lines in this interval.");
            cell.colSpan = 3;
            elements.categoriesBody.replaceChildren(row);
        }
    }

    function renderStaff(staffData) {
        const staff = Array.isArray(staffData.staff) ? staffData.staff : [];
        setText("cashier-summary", `${formatCount(staff.length)} staff`);
        replaceDataList(
            elements.cashierList,
            staff.map((member) => ({
                title: member.name || "Unnamed staff member",
                subtitle: (
                    `${humanize(member.role)} · ${formatCount(member.orders_total)} orders`
                    + ` · ${formatCount(member.orders_cancelled)} cancelled`
                ),
                value: formatMoney(member.revenue),
                tone: "violet",
            })),
            "No staff activity in this interval.",
        );

        const ranked = staff
            .filter((member) => numeric(member.revenue) !== null)
            .sort((left, right) => numeric(right.revenue) - numeric(left.revenue));
        const best = ranked[0];
        elements.bestCashier.hidden = !best;
        if (best) {
            const name = best.name || "Unnamed staff member";
            setText("best-cashier-avatar", name.trim().charAt(0).toUpperCase() || "—");
            setText("best-cashier-name", name);
            setText("best-cashier-orders", formatCount(best.orders_total));
            setText("best-cashier-revenue", formatMoney(best.revenue));
        }

        replaceRows(elements.staffBody, staff, (member) => {
            const row = document.createElement("tr");
            appendCell(row, member.name || "Unnamed staff member");
            appendCell(row, humanize(member.role));
            appendCell(row, formatCount(member.orders_total), "numeric");
            appendCell(row, formatCount(member.orders_paid), "numeric");
            appendCell(row, formatCount(member.orders_cancelled), "numeric");
            appendCell(row, formatMoney(member.gross_revenue), "numeric");
            appendCell(row, formatMoney(member.refund_amount), "numeric");
            appendCell(row, formatMoney(member.revenue), "numeric");
            appendCell(row, member.hours_worked === null ? "—" : String(member.hours_worked), "numeric");
            return row;
        });
        if (!staff.length) {
            const row = document.createElement("tr");
            const cell = appendCell(row, "No staff activity in this interval.");
            cell.colSpan = 9;
            elements.staffBody.replaceChildren(row);
        }
    }

    function renderExpenses(expenseData) {
        const expenses = Array.isArray(expenseData.expenses) ? expenseData.expenses : [];
        replaceRows(elements.expensesBody, expenses, (expense) => {
            const row = document.createElement("tr");
            appendCell(row, formatDateTime(expense.created_at));
            appendCell(row, expense.cashier_name || "—");
            appendCell(row, expense.category || "Uncategorized");
            appendCell(row, expense.comment || "—");
            appendCell(row, expense.shift_id ? `#${expense.shift_id}` : "—");
            appendCell(row, formatMoney(expense.amount), "numeric");
            return row;
        });
        elements.expensesEmpty.hidden = expenses.length !== 0;

        const pagination = expenseData.pagination || {};
        const total = numeric(pagination.total);
        const shown = expenses.length;
        elements.expensePaginationNote.textContent = total !== null && total > shown
            ? `Showing the newest ${shown} of ${formatCount(total)} expenses.`
            : `${formatCount(total === null ? shown : total)} expense records.`;
    }

    function renderDoughnut({
        chartId,
        emptyId,
        legend,
        labels,
        values,
        valueFormatter,
    }) {
        const rows = labels
            .map((label, index) => ({
                label,
                value: numeric(values[index]) ?? 0,
                color: CHART_COLORS[index % CHART_COLORS.length],
            }))
            .filter((row) => row.value > 0);
        const canvas = element(chartId);
        const empty = element(emptyId);
        const total = rows.reduce((sum, row) => sum + row.value, 0);
        canvas.hidden = rows.length === 0;
        empty.hidden = rows.length !== 0;
        replaceLegend(
            legend,
            rows.slice(0, 6).map((row) => ({
                label: row.label,
                value: valueFormatter(row.value, total),
                color: row.color,
            })),
        );
        if (!rows.length) {
            destroyChart(chartId);
            return;
        }
        createChart(chartId, {
            type: "doughnut",
            data: {
                labels: rows.map((row) => row.label),
                datasets: [{
                    data: rows.map((row) => row.value),
                    backgroundColor: rows.map((row) => row.color),
                    borderColor: "rgba(0, 0, 0, 0.3)",
                    borderWidth: 2,
                    hoverBorderColor: "#ffffff",
                    hoverBorderWidth: 3,
                    hoverOffset: 10,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "65%",
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: "rgba(15, 23, 42, 0.96)",
                        padding: 12,
                        cornerRadius: 10,
                        callbacks: {
                            label: (context) => valueFormatter(context.raw, total),
                        },
                    },
                },
            },
        });
    }

    function renderCharts(dashboard, sales) {
        if (!window.Chart) {
            return;
        }
        window.Chart.defaults.color = "#94a3b8";
        window.Chart.defaults.borderColor = "rgba(255, 255, 255, 0.05)";
        window.Chart.defaults.font.family = "'Plus Jakarta Sans', sans-serif";

        const products = Array.isArray(dashboard.top_products) ? dashboard.top_products : [];
        const chartProducts = products.filter(
            (product) => (
                (numeric(product.quantity) ?? 0) > 0
                && (numeric(product.revenue) ?? 0) > 0
            ),
        );
        const shownProductUnits = chartProducts.reduce(
            (sum, product) => sum + (numeric(product.quantity) ?? 0),
            0,
        );
        const omittedProducts = products.length - chartProducts.length;
        setText(
            "product-items-badge",
            `${formatCount(shownProductUnits)} shown units`,
        );
        setText(
            "product-chart-note",
            "Only server-provided top-five rows with positive net revenue and positive "
            + `net units are charted. ${formatCount(omittedProducts)} non-positive or `
            + "refund-heavy rows were omitted; returned rows remain in the detailed ledger.",
        );
        renderDoughnut({
            chartId: "product-pie-chart",
            emptyId: "product-chart-empty",
            legend: elements.productLegend,
            labels: chartProducts.map(
                (product) => product.product_name || "Unnamed product",
            ),
            values: chartProducts.map((product) => product.quantity),
            valueFormatter: (value, total) => (
                total > 0 ? `${((value / total) * 100).toFixed(1)}%` : "0.0%"
            ),
        });

        const categories = Array.isArray(dashboard.category_stats)
            ? dashboard.category_stats
            : [];
        const positiveCategories = categories.filter(
            (category) => (numeric(category.revenue) ?? 0) > 0,
        );
        const omittedCategories = categories.length - positiveCategories.length;
        setText(
            "category-chart-note",
            "The doughnut includes positive net-revenue categories only. "
            + `${formatCount(omittedCategories)} zero or negative refund-heavy rows were `
            + "omitted; all returned categories remain in the detailed ledger.",
        );
        renderDoughnut({
            chartId: "category-pie-chart",
            emptyId: "category-chart-empty",
            legend: elements.categoryLegend,
            labels: positiveCategories.map(
                (category) => category.category || "Uncategorized",
            ),
            values: positiveCategories.map((category) => category.revenue),
            valueFormatter: (value) => formatCompactNumber(value),
        });

        const channelDays = Array.isArray(sales.channelDays) ? sales.channelDays : [];
        const channelTotals = channelDays.reduce(
            (totals, day) => ({
                hall: totals.hall + (numeric(day.hall) ?? 0),
                delivery: totals.delivery + (numeric(day.delivery) ?? 0),
                pickup: totals.pickup + (numeric(day.pickup) ?? 0),
            }),
            { hall: 0, delivery: 0, pickup: 0 },
        );
        setText("order-hall-count", formatCount(channelTotals.hall));
        setText("order-delivery-count", formatCount(channelTotals.delivery));
        setText("order-pickup-count", formatCount(channelTotals.pickup));
        createChart("order-type-chart", {
            type: "bar",
            data: {
                labels: ["Dine-in", "Delivery", "Pickup"],
                datasets: [{
                    data: [channelTotals.hall, channelTotals.delivery, channelTotals.pickup],
                    backgroundColor: [
                        "rgba(99, 102, 241, 0.5)",
                        "rgba(245, 158, 11, 0.5)",
                        "rgba(16, 185, 129, 0.5)",
                    ],
                    borderColor: ["#818cf8", "#fbbf24", "#34d399"],
                    borderWidth: 2,
                    borderRadius: 8,
                }],
            },
            options: {
                ...lineChartOptions(),
                plugins: { legend: { display: false } },
            },
        });

        const labels = Array.isArray(sales.dayLabels) ? sales.dayLabels : [];
        const revenue = numericSeries(sales.revenue30);
        const orderSeries = channelDays.map(
            (day) => (
                (numeric(day.hall) ?? 0)
                + (numeric(day.delivery) ?? 0)
                + (numeric(day.pickup) ?? 0)
            ),
        );
        createChart("revenue-chart", {
            type: "line",
            data: {
                labels,
                datasets: [{
                    label: "Net revenue",
                    data: revenue,
                    borderColor: "#10b981",
                    backgroundColor: "rgba(16, 185, 129, 0.1)",
                    borderWidth: 2,
                    pointBackgroundColor: "#10b981",
                    pointBorderColor: "#ffffff",
                    pointRadius: 3,
                    tension: 0.35,
                    fill: true,
                }],
            },
            options: lineChartOptions({ money: true }),
        });
        createChart("orders-chart", {
            type: "line",
            data: {
                labels,
                datasets: [{
                    label: "Non-cancelled classified orders (created_at)",
                    data: orderSeries,
                    borderColor: "#6366f1",
                    backgroundColor: "rgba(99, 102, 241, 0.1)",
                    borderWidth: 2,
                    pointBackgroundColor: "#6366f1",
                    pointBorderColor: "#ffffff",
                    pointRadius: 3,
                    tension: 0.35,
                    fill: true,
                }],
            },
            options: lineChartOptions(),
        });

        const previousRevenue = numericSeries(
            Array.isArray(sales.lastMonthRev)
                ? sales.lastMonthRev
                : sales.previous_period && sales.previous_period.revenue_series,
        );
        createChart("comparison-chart", {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: "Selected period",
                        data: revenue,
                        borderColor: "#8b5cf6",
                        backgroundColor: "rgba(139, 92, 246, 0.08)",
                        borderWidth: 2,
                        pointRadius: 3,
                        tension: 0.35,
                        fill: true,
                    },
                    {
                        label: "Previous equal period",
                        data: previousRevenue,
                        borderColor: "#f59e0b",
                        borderDash: [6, 5],
                        borderWidth: 2,
                        pointRadius: 2,
                        tension: 0.35,
                        fill: false,
                    },
                ],
            },
            options: lineChartOptions({ money: true, legend: true }),
        });

        const payments = dashboard.payment_breakdown || {};
        const tenderLabels = ["Cash", "All cards", "Payme"];
        const tenderValues = [
            numeric(payments.cash) ?? 0,
            numeric(payments.card) ?? 0,
            numeric(payments.payme) ?? 0,
        ];
        if (Object.prototype.hasOwnProperty.call(payments, "unknown")) {
            tenderLabels.push("Unknown");
            tenderValues.push(numeric(payments.unknown) ?? 0);
        }
        createChart("tender-chart", {
            type: "bar",
            data: {
                labels: tenderLabels,
                datasets: [{
                    label: "Net attributed amount",
                    data: tenderValues,
                    backgroundColor: tenderLabels.map(
                        (_label, index) => `${CHART_COLORS[index % CHART_COLORS.length]}88`,
                    ),
                    borderColor: tenderLabels.map(
                        (_label, index) => CHART_COLORS[index % CHART_COLORS.length],
                    ),
                    borderWidth: 2,
                    borderRadius: 7,
                }],
            },
            options: {
                ...lineChartOptions({ money: true }),
                indexAxis: "y",
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: "rgba(15, 23, 42, 0.96)",
                        padding: 12,
                        cornerRadius: 10,
                        callbacks: {
                            label: (context) => formatMoney(context.parsed.x),
                        },
                    },
                },
                scales: {
                    x: {
                        grid: { color: "rgba(255, 255, 255, 0.035)" },
                        ticks: { callback: (value) => formatCompactNumber(value) },
                    },
                    y: { grid: { display: false } },
                },
            },
        });
    }

    function completeExpectedCashEvidence(shift) {
        return (
            shift
            && shift.financial_evidence_available === true
            && shift.cash_to_receive_complete === true
            && numeric(shift.expected_cash) !== null
        );
    }

    function renderActiveShifts(activeShifts) {
        if (!Array.isArray(activeShifts)) {
            throw new Error("Active-shift evidence returned an invalid response.");
        }
        const shifts = activeShifts;
        replaceRows(elements.activeShiftsBody, shifts, (shift) => {
            const row = document.createElement("tr");
            appendCell(row, shift.user && shift.user.name ? shift.user.name : "Unnamed cashier");
            appendCell(row, formatDateTime(shift.start_time));

            const complete = completeExpectedCashEvidence(shift);
            const cashCell = appendCell(
                row,
                complete ? formatMoney(shift.expected_cash) : "Evidence incomplete",
                complete ? "numeric" : "evidence-incomplete",
            );
            if (!complete) {
                cashCell.setAttribute(
                    "title",
                    "The server did not provide complete drawer attribution evidence.",
                );
            }
            appendCell(row, humanize(shift.expected_cash_source));
            const evidenceCell = appendCell(row, "");
            appendBadge(
                evidenceCell,
                complete ? "Complete" : "Evidence incomplete",
                complete ? "good" : "bad",
            );
            return row;
        });
        elements.activeShiftsEmpty.hidden = shifts.length !== 0;
    }

    function renderActiveShiftUnavailable(message) {
        const row = document.createElement("tr");
        const cell = appendCell(row, message || "Live drawer evidence is unavailable.");
        cell.colSpan = 5;
        cell.className = "evidence-incomplete";
        elements.activeShiftsBody.replaceChildren(row);
        elements.activeShiftsEmpty.hidden = true;
    }

    async function fetchActiveShifts() {
        const data = await apiRequest(API.activeShifts);
        renderActiveShifts(data);
        return data;
    }

    async function loadDashboard() {
        elements.reportContent.hidden = true;
        const dateFrom = elements.dateFrom.value;
        const dateTo = elements.dateTo.value;
        if (!dateFrom || !dateTo) {
            setMessage("Choose both operating dates.", "error");
            return;
        }
        if (dateFrom > dateTo) {
            setMessage("The From date must be on or before the To date.", "error");
            return;
        }

        const requestId = ++currentRequest;
        elements.applyRange.disabled = true;
        setMessage("Loading canonical reporting data…");
        try {
            const [dashboard, sales, expenses, staff, activeShifts] = await Promise.all([
                apiRequest(endpointWithRange(API.dashboard, dateFrom, dateTo)),
                apiRequest(endpointWithRange(API.sales, dateFrom, dateTo, { granularity: "day" })),
                apiRequest(endpointWithRange(API.expenses, dateFrom, dateTo, { limit: 200 })),
                apiRequest(endpointWithRange(API.staff, dateFrom, dateTo)),
                apiRequest(API.activeShifts),
            ]);
            if (requestId !== currentRequest) {
                return;
            }
            assertMatchingRanges(dashboard, sales, expenses, staff);
            renderRange(dashboard, sales, expenses, staff);
            renderSummary(dashboard, expenses);
            renderTender(dashboard);
            renderPrevious(dashboard);
            renderDailySales(sales);
            renderProducts(dashboard);
            renderCategories(dashboard);
            renderStaff(staff);
            renderExpenses(expenses);
            renderActiveShifts(activeShifts);
            renderCharts(dashboard, sales);
            elements.reportContent.hidden = false;
            setMessage(
                "Canonical data loaded from the cloud copy; sync freshness is not independently verified.",
                "success",
            );
        } catch (error) {
            if (requestId !== currentRequest) {
                return;
            }
            if (error instanceof AuthenticationRequired) {
                showLogin("Your administrator session expired. Sign in again.");
                return;
            }
            setMessage(error.message || "Unable to load the report.", "error");
        } finally {
            if (requestId === currentRequest) {
                elements.applyRange.disabled = false;
            }
        }
    }

    async function establishSession() {
        try {
            const user = await apiRequest(API.me);
            showDashboard(user);
            await loadDashboard();
        } catch (error) {
            if (error instanceof AuthenticationRequired) {
                showLogin();
                return;
            }
            showLogin("Unable to verify the administrator session.");
        }
    }

    async function handleLogin(event) {
        event.preventDefault();
        elements.loginButton.disabled = true;
        elements.loginError.hidden = true;
        const email = elements.loginEmail.value.trim();
        const password = elements.loginPassword.value;
        elements.loginPassword.value = "";
        try {
            await apiRequest(API.login, {
                method: "POST",
                body: JSON.stringify({
                    email,
                    password,
                }),
            });
            await establishSession();
        } catch (error) {
            showLogin(error.message || "Sign-in failed.");
        } finally {
            elements.loginButton.disabled = false;
        }
    }

    async function handleLogout() {
        try {
            await apiRequest(API.logout, { method: "POST" });
        } catch (_error) {
            // The local shell still returns to sign-in if the session already expired.
        }
        currentRequest += 1;
        showLogin();
    }

    async function handleDrawerRefresh() {
        elements.refreshDrawer.disabled = true;
        renderActiveShiftUnavailable("Refreshing live drawer evidence…");
        setMessage("Refreshing active shift evidence…");
        try {
            await fetchActiveShifts();
            setMessage("Live shift evidence refreshed.", "success");
        } catch (error) {
            if (error instanceof AuthenticationRequired) {
                showLogin("Your administrator session expired. Sign in again.");
                return;
            }
            renderActiveShiftUnavailable(
                "Live drawer evidence is unavailable. The previous value was cleared.",
            );
            setMessage(error.message || "Unable to refresh active shifts.", "error");
        } finally {
            elements.refreshDrawer.disabled = false;
        }
    }

    function bindElements() {
        Object.assign(elements, {
            loginView: element("login-view"),
            dashboardView: element("dashboard-view"),
            loginForm: element("login-form"),
            loginEmail: element("login-email"),
            loginPassword: element("login-password"),
            loginButton: element("login-button"),
            loginError: element("login-error"),
            logoutButton: element("logout-button"),
            rangeForm: element("range-form"),
            reportContent: element("report-content"),
            dateFrom: element("date-from"),
            dateTo: element("date-to"),
            applyRange: element("apply-range"),
            pageMessage: element("page-message"),
            rangeConsistency: element("range-consistency"),
            unknownWarning: element("unknown-warning"),
            tenderDetail: element("tender-detail"),
            previousDetail: element("previous-detail"),
            dailySalesBody: element("daily-sales-body"),
            productsBody: element("products-body"),
            categoriesBody: element("categories-body"),
            staffBody: element("staff-body"),
            expensesBody: element("expenses-body"),
            expensesEmpty: element("expenses-empty"),
            expensePaginationNote: element("expense-pagination-note"),
            activeShiftsBody: element("active-shifts-body"),
            activeShiftsEmpty: element("active-shifts-empty"),
            refreshDrawer: element("refresh-drawer"),
            productLegend: element("product-legend"),
            categoryLegend: element("category-legend"),
            productList: element("product-list"),
            cashierList: element("cashier-list"),
            bestCashier: element("best-cashier"),
            presetButtons: Array.from(document.querySelectorAll("[data-range-preset]")),
        });
    }

    function start() {
        bindElements();
        const defaultDate = tashkentBusinessDate();
        elements.dateFrom.value = defaultDate;
        elements.dateTo.value = defaultDate;
        updateHeaderTime();
        elements.loginForm.addEventListener("submit", handleLogin);
        elements.logoutButton.addEventListener("click", handleLogout);
        elements.rangeForm.addEventListener("submit", (event) => {
            event.preventDefault();
            syncPresetState();
            loadDashboard();
        });
        [elements.dateFrom, elements.dateTo].forEach((input) => {
            input.addEventListener("change", () => {
                currentRequest += 1;
                elements.applyRange.disabled = false;
                syncPresetState();
                elements.reportContent.hidden = true;
                setMessage("Date selection changed. Apply the range to load verified figures.");
            });
        });
        elements.presetButtons.forEach((button) => {
            button.addEventListener("click", () => {
                const preset = button.dataset.rangePreset || "today";
                const [dateFrom, dateTo] = presetRange(preset);
                elements.dateFrom.value = dateFrom;
                elements.dateTo.value = dateTo;
                setActivePreset(preset);
                loadDashboard();
            });
        });
        elements.refreshDrawer.addEventListener("click", handleDrawerRefresh);
        establishSession();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
        start();
    }
})();
