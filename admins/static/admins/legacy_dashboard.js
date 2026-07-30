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

    class AuthenticationRequired extends Error {}

    const elements = {};
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
            `${formatCount(dashboard.paid_orders)} paid · ${formatCount(dashboard.cancelled)} cancelled`,
        );
        setText("kpi-units", formatCount(dashboard.units_sold));

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
            elements.reportContent.hidden = false;
            setMessage(
                "Canonical data loaded from the cloud copy; sync freshness is not independently verified.",
                "success",
            );
        } catch (error) {
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
        try {
            await apiRequest(API.login, {
                method: "POST",
                body: JSON.stringify({
                    email: elements.loginEmail.value.trim(),
                    password: elements.loginPassword.value,
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
        });
    }

    function start() {
        bindElements();
        const defaultDate = tashkentBusinessDate();
        elements.dateFrom.value = defaultDate;
        elements.dateTo.value = defaultDate;
        elements.loginForm.addEventListener("submit", handleLogin);
        elements.logoutButton.addEventListener("click", handleLogout);
        elements.rangeForm.addEventListener("submit", (event) => {
            event.preventDefault();
            loadDashboard();
        });
        [elements.dateFrom, elements.dateTo].forEach((input) => {
            input.addEventListener("change", () => {
                elements.reportContent.hidden = true;
                setMessage("Date selection changed. Apply the range to load verified figures.");
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
