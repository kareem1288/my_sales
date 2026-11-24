"""Built-in reports replacing the ERPNext query/script reports used by /reports.

Each report exposes Frappe-style filter definitions (served through
`frappe.desk.query_report.get_script`, which the existing reports.js loads) and
returns `{"columns": [...], "result": [...]}` like `frappe.desk.query_report.run`.

The accounting reports (P&L, General Ledger, Trial Balance) are derived from
bills, payments and expenses using a small fixed chart of accounts, since this
app does not keep a full double-entry ledger like ERPNext.
"""

import re
from datetime import date

from sqlalchemy import select

from ..models import CustomReport, Customer, Expense, Item, Payment, SalesInvoice, SalesInvoiceItem
from ..registry import whitelist
from ..setup import DEFAULT_REPORTS, ensure_default_reports
from ..utils import AppError, add_days, cint, first_day, flt, getdate, last_day, parse_json, today

REPORTS = {}


def report(name, ref_doctype, description, filters):
	def decorator(func):
		REPORTS[name] = {"run": func, "ref_doctype": ref_doctype, "description": description, "filters": filters}
		return func

	return decorator


def col(fieldname, label, fieldtype="Data", width=120):
	return {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "width": width}


def _month_ago():
	return str(add_days(today(), -30))


def _fy_start():
	current = today()
	year = current.year if current.month >= 4 else current.year - 1
	return f"{year}-04-01"


COMPANY = {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1}


def FROM(default=None):
	return {"fieldname": "from_date", "label": "From Date", "fieldtype": "Date", "default": default or _month_ago()}


def TO():
	return {"fieldname": "to_date", "label": "To Date", "fieldtype": "Date", "default": str(today())}


def _range(filters, start_key="from_date", end_key="to_date", default_start=None):
	try:
		start = getdate(filters.get(start_key) or default_start or _month_ago())
		end = getdate(filters.get(end_key) or today())
	except ValueError:
		raise AppError("Select valid dates.")
	if start > end:
		raise AppError("From Date cannot be after To Date.")
	return start, end


def _invoices(ctx, company, start, end, customer=None):
	query = select(SalesInvoice).where(
		SalesInvoice.company == company, SalesInvoice.docstatus == 1, SalesInvoice.posting_date.between(start, end)
	)
	if customer:
		query = query.where(SalesInvoice.customer == customer)
	return ctx.db.execute(query.order_by(SalesInvoice.posting_date, SalesInvoice.posting_time)).scalars().all()


def _payments_by_invoice(ctx, names):
	modes = {}
	if names:
		for invoice, mode in ctx.db.execute(
			select(Payment.invoice, Payment.mode_of_payment).where(Payment.invoice.in_(names))
		).all():
			modes.setdefault(invoice, [])
			if mode not in modes[invoice]:
				modes[invoice].append(mode)
	return modes


# ------------------------------------------------------------------ reports


@report("Sales Register", "Sales Invoice", "Review invoices, taxable sales and collected revenue.", [
	FROM(), TO(),
	{"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer"},
	COMPANY,
	{"fieldname": "mode_of_payment", "label": "Mode of Payment", "fieldtype": "Select", "options": "\nCash\nUPI\nCard"},
])
def sales_register(ctx, company, filters):
	start, end = _range(filters)
	invoices = _invoices(ctx, company, start, end, filters.get("customer"))
	modes = _payments_by_invoice(ctx, [inv.name for inv in invoices])
	wanted = filters.get("mode_of_payment")
	rows = []
	for inv in invoices:
		inv_modes = modes.get(inv.name, [])
		if wanted and wanted not in inv_modes:
			continue
		rows.append({
			"voucher_no": inv.name, "posting_date": str(inv.posting_date), "customer": inv.customer,
			"customer_name": inv.customer_name, "mode_of_payment": ", ".join(inv_modes), "total_qty": inv.total_qty,
			"net_total": inv.net_total, "discount_amount": inv.discount_amount, "grand_total": inv.rounded_total,
			"outstanding_amount": inv.outstanding_amount,
		})
	columns = [
		col("voucher_no", "Invoice", "Link", 150), col("posting_date", "Posting Date", "Date"),
		col("customer", "Customer", "Link", 160), col("customer_name", "Customer Name", "Data", 160),
		col("mode_of_payment", "Payment Method"), col("total_qty", "Qty", "Float", 80),
		col("net_total", "Net Total", "Currency"), col("discount_amount", "Discount", "Currency"),
		col("grand_total", "Grand Total", "Currency"), col("outstanding_amount", "Outstanding", "Currency"),
	]
	return columns, rows


@report("Item-wise Sales Register", "Sales Invoice", "See item quantities, rates and sales performance.", [
	FROM(), TO(),
	{"fieldname": "item_code", "label": "Item", "fieldtype": "Link", "options": "Item"},
	{"fieldname": "item_group", "label": "Item Group", "fieldtype": "Link", "options": "Item Group"},
	COMPANY,
])
def item_wise_sales(ctx, company, filters):
	start, end = _range(filters)
	query = (
		select(SalesInvoiceItem, SalesInvoice).join(SalesInvoice)
		.where(SalesInvoice.company == company, SalesInvoice.docstatus == 1, SalesInvoice.posting_date.between(start, end))
		.order_by(SalesInvoice.posting_date, SalesInvoice.name, SalesInvoiceItem.idx)
	)
	if filters.get("item_code"):
		query = query.where(SalesInvoiceItem.item_code == filters["item_code"])
	if filters.get("item_group"):
		query = query.where(SalesInvoiceItem.item_group == filters["item_group"])
	rows = [
		{
			"item_code": item.item_code, "item_name": item.item_name, "item_group": item.item_group,
			"invoice": inv.name, "posting_date": str(inv.posting_date), "customer": inv.customer,
			"qty": item.qty, "rate": item.rate, "amount": item.amount,
		}
		for item, inv in ctx.db.execute(query).all()
	]
	columns = [
		col("item_code", "Item Code", "Link", 140), col("item_name", "Item Name", "Data", 160),
		col("item_group", "Item Group", "Link"), col("invoice", "Invoice", "Link", 150),
		col("posting_date", "Posting Date", "Date"), col("customer", "Customer", "Link", 150),
		col("qty", "Qty", "Float", 80), col("rate", "Rate", "Currency"), col("amount", "Amount", "Currency"),
	]
	return columns, rows


@report("Customer Ledger Summary", "Sales Invoice", "Track customer billing, payments and outstanding balances.", [
	COMPANY, FROM(), TO(),
	{"fieldname": "party", "label": "Customer", "fieldtype": "Link", "options": "Customer"},
])
def customer_ledger(ctx, company, filters):
	start, end = _range(filters)
	party = filters.get("party") or filters.get("customer")

	def totals(model, amount_col, cond):
		query = select(model.customer, amount_col).where(model.company == company, cond)
		if party:
			query = query.where(model.customer == party)
		return ctx.db.execute(query).all()

	ledger = {}

	def bucket(customer):
		return ledger.setdefault(customer, {"opening_balance": 0, "invoiced": 0, "paid": 0})

	for customer, amount in totals(SalesInvoice, SalesInvoice.rounded_total,
			(SalesInvoice.docstatus == 1) & (SalesInvoice.posting_date < start)):
		bucket(customer)["opening_balance"] += flt(amount)
	for customer, amount in totals(Payment, Payment.amount, Payment.posting_date < start):
		bucket(customer)["opening_balance"] -= flt(amount)
	for customer, amount in totals(SalesInvoice, SalesInvoice.rounded_total,
			(SalesInvoice.docstatus == 1) & SalesInvoice.posting_date.between(start, end)):
		bucket(customer)["invoiced"] += flt(amount)
	for customer, amount in totals(Payment, Payment.amount, Payment.posting_date.between(start, end)):
		bucket(customer)["paid"] += flt(amount)

	names = dict(ctx.db.execute(
		select(Customer.name, Customer.customer_name).where(Customer.company == company)
	).all())
	rows = []
	for customer, values in sorted(ledger.items()):
		if not any(values.values()):
			continue
		closing = values["opening_balance"] + values["invoiced"] - values["paid"]
		rows.append({
			"party": customer, "customer_name": names.get(customer, customer),
			"opening_balance": round(values["opening_balance"], 2), "invoiced": round(values["invoiced"], 2),
			"paid": round(values["paid"], 2), "closing_balance": round(closing, 2),
		})
	columns = [
		col("party", "Customer", "Link", 160), col("customer_name", "Customer Name", "Data", 160),
		col("opening_balance", "Opening Balance", "Currency"), col("invoiced", "Invoiced Amount", "Currency"),
		col("paid", "Paid Amount", "Currency"), col("closing_balance", "Closing Balance", "Currency"),
	]
	return columns, rows


@report("Accounts Receivable", "Sales Invoice", "Monitor unpaid invoices, due dates and outstanding amounts.", [
	COMPANY,
	{"fieldname": "report_date", "label": "Posting Date", "fieldtype": "Date", "default": str(today())},
	{"fieldname": "party", "label": "Customer", "fieldtype": "Link", "options": "Customer"},
	{"fieldname": "ageing_based_on", "label": "Ageing Based On", "fieldtype": "Select", "options": "Posting Date\nDue Date", "default": "Posting Date"},
])
def accounts_receivable(ctx, company, filters):
	report_date = getdate(filters.get("report_date") or today())
	query = select(SalesInvoice).where(
		SalesInvoice.company == company, SalesInvoice.docstatus == 1,
		SalesInvoice.posting_date <= report_date, SalesInvoice.outstanding_amount > 0.005,
	)
	if filters.get("party"):
		query = query.where(SalesInvoice.customer == filters["party"])
	rows = []
	for inv in ctx.db.execute(query.order_by(SalesInvoice.posting_date)).scalars():
		age = (report_date - inv.posting_date).days
		rows.append({
			"posting_date": str(inv.posting_date), "party": inv.customer, "customer_name": inv.customer_name,
			"voucher_no": inv.name, "due_date": str(inv.posting_date), "invoiced": inv.rounded_total,
			"paid": inv.paid_amount, "outstanding": inv.outstanding_amount, "age": age,
			"status": "Overdue" if age > 30 else "Unpaid",
		})
	columns = [
		col("posting_date", "Posting Date", "Date"), col("party", "Customer", "Link", 160),
		col("customer_name", "Customer Name", "Data", 160), col("voucher_no", "Invoice", "Link", 150),
		col("due_date", "Due Date", "Date"), col("invoiced", "Invoiced Amount", "Currency"),
		col("paid", "Paid Amount", "Currency"), col("outstanding", "Outstanding Amount", "Currency"),
		col("age", "Age (Days)", "Int", 90), col("status", "Status"),
	]
	return columns, rows


def _periods(start, end, periodicity):
	months = {"Monthly": 1, "Quarterly": 3, "Half-Yearly": 6, "Yearly": 12}.get(periodicity or "Monthly", 1)
	periods, cursor = [], first_day(start)
	while cursor <= end:
		stop = _end_of_month_after(cursor, months - 1)
		periods.append((max(cursor, start), min(stop, end)))
		cursor = add_days(stop, 1)
	return periods


def _end_of_month_after(value, months):
	"""Last day of the month `months` months after `value`'s month."""
	month_index = value.month - 1 + months
	return last_day(date(value.year + month_index // 12, month_index % 12 + 1, 1))


@report("Profit and Loss Statement", "GL Entry", "Compare income, expenses and net profit across the selected period.", [
	COMPANY,
	{"fieldname": "filter_based_on", "label": "Filter Based On", "fieldtype": "Select", "options": "Date Range", "default": "Date Range"},
	{"fieldname": "period_start_date", "label": "From Date", "fieldtype": "Date", "default": _fy_start},
	{"fieldname": "period_end_date", "label": "To Date", "fieldtype": "Date", "default": lambda: str(today())},
	{"fieldname": "periodicity", "label": "Periodicity", "fieldtype": "Select", "options": "Monthly\nQuarterly\nHalf-Yearly\nYearly", "default": "Monthly"},
	{"fieldname": "accumulated_values", "label": "Accumulated Values", "fieldtype": "Check", "default": 0},
])
def profit_and_loss(ctx, company, filters):
	start, end = _range(filters, "period_start_date", "period_end_date", _fy_start())
	if (end - start).days > 3 * 366:
		raise AppError("Select a period of at most three years.")
	sales = ctx.db.execute(
		select(SalesInvoice.posting_date, SalesInvoice.rounded_total).where(
			SalesInvoice.company == company, SalesInvoice.docstatus == 1, SalesInvoice.posting_date.between(start, end))
	).all()
	expenses = ctx.db.execute(
		select(Expense.posting_date, Expense.amount).where(Expense.company == company, Expense.posting_date.between(start, end))
	).all()
	accumulate = cint(filters.get("accumulated_values"))
	rows, running_income, running_expense = [], 0, 0
	for period_start, period_end in _periods(start, end, filters.get("periodicity")):
		income = sum(flt(v) for d, v in sales if period_start <= d <= period_end)
		expense = sum(flt(v) for d, v in expenses if period_start <= d <= period_end)
		running_income, running_expense = running_income + income, running_expense + expense
		if accumulate:
			income, expense = running_income, running_expense
		label = period_start.strftime("%b %Y") if period_start.month == period_end.month and period_start.year == period_end.year \
			else f"{period_start.strftime('%b %Y')} – {period_end.strftime('%b %Y')}"
		rows.append({
			"period": label, "from_date": str(period_start), "to_date": str(period_end), "income": round(income, 2),
			"expense": round(expense, 2), "net_profit_loss": round(income - expense, 2),
		})
	columns = [
		col("period", "Period", "Data", 160), col("from_date", "From", "Date"), col("to_date", "To", "Date"),
		col("income", "Income", "Currency"), col("expense", "Expense", "Currency"),
		col("net_profit_loss", "Net Profit/Loss", "Currency"),
	]
	return columns, rows


def _ledger_entries(ctx, company, start=None, end=None, before=None):
	"""Derive double-entry lines from bills, payments and expenses."""
	def window(column):
		if before is not None:
			return column < before
		return column.between(start, end)

	entries = []
	for inv in ctx.db.execute(select(SalesInvoice).where(
		SalesInvoice.company == company, SalesInvoice.docstatus == 1, window(SalesInvoice.posting_date)
	)).scalars():
		base = {"posting_date": inv.posting_date, "voucher_type": "Sales Invoice", "voucher_no": inv.name, "party": inv.customer}
		entries.append({**base, "account": "Debtors", "debit": inv.rounded_total, "credit": 0})
		entries.append({**base, "account": "Sales", "debit": 0, "credit": inv.net_total})
		if inv.discount_amount:
			entries.append({**base, "account": "Discount Allowed", "debit": inv.discount_amount, "credit": 0})
		if inv.rounding_adjustment:
			adj = inv.rounding_adjustment
			entries.append({**base, "account": "Round Off", "debit": 0 if adj > 0 else -adj, "credit": adj if adj > 0 else 0})
	for pay in ctx.db.execute(select(Payment).where(Payment.company == company, window(Payment.posting_date))).scalars():
		base = {"posting_date": pay.posting_date, "voucher_type": "Payment Entry", "voucher_no": pay.name, "party": pay.customer}
		account = "Cash" if pay.mode_of_payment == "Cash" else f"Bank - {pay.mode_of_payment}"
		entries.append({**base, "account": account, "debit": pay.amount, "credit": 0})
		entries.append({**base, "account": "Debtors", "debit": 0, "credit": pay.amount})
	for exp in ctx.db.execute(select(Expense).where(Expense.company == company, window(Expense.posting_date))).scalars():
		base = {"posting_date": exp.posting_date, "voucher_type": "Expense", "voucher_no": exp.name, "party": ""}
		entries.append({**base, "account": "Expenses", "debit": exp.amount, "credit": 0})
		entries.append({**base, "account": "Cash", "debit": 0, "credit": exp.amount})
	entries.sort(key=lambda e: (e["posting_date"], e["voucher_no"]))
	return entries


@report("General Ledger", "GL Entry", "Review account-wise debits, credits, vouchers and running balances.", [
	COMPANY, FROM(), TO(),
	{"fieldname": "account", "label": "Account", "fieldtype": "Select", "options": "\nCash\nDebtors\nSales\nExpenses\nDiscount Allowed\nRound Off\nBank - UPI\nBank - Card"},
	{"fieldname": "party", "label": "Party", "fieldtype": "Link", "options": "Customer"},
	{"fieldname": "voucher_no", "label": "Voucher No", "fieldtype": "Data"},
])
def general_ledger(ctx, company, filters):
	start, end = _range(filters)
	rows, balance = [], 0
	for entry in _ledger_entries(ctx, company, start, end):
		if filters.get("account") and entry["account"] != filters["account"]:
			continue
		if filters.get("party") and entry["party"] != filters["party"]:
			continue
		if filters.get("voucher_no") and filters["voucher_no"] not in entry["voucher_no"]:
			continue
		balance += flt(entry["debit"]) - flt(entry["credit"])
		rows.append({**entry, "posting_date": str(entry["posting_date"]), "balance": round(balance, 2)})
	columns = [
		col("posting_date", "Posting Date", "Date"), col("account", "Account", "Data", 150),
		col("debit", "Debit", "Currency"), col("credit", "Credit", "Currency"), col("balance", "Balance", "Currency"),
		col("voucher_type", "Voucher Type"), col("voucher_no", "Voucher No", "Data", 150), col("party", "Party", "Data", 150),
	]
	return columns, rows


@report("Trial Balance", "GL Entry", "Compare opening, debit, credit and closing balances for every account.", [
	COMPANY,
	{"fieldname": "from_date", "label": "From Date", "fieldtype": "Date", "default": _fy_start},
	TO(),
	{"fieldname": "show_net_values", "label": "Show Net Values", "fieldtype": "Check", "default": 0},
])
def trial_balance(ctx, company, filters):
	start, end = _range(filters, default_start=_fy_start())
	accounts = {}

	def bucket(name):
		return accounts.setdefault(name, {"opening": 0.0, "debit": 0.0, "credit": 0.0})

	for entry in _ledger_entries(ctx, company, before=start):
		bucket(entry["account"])["opening"] += flt(entry["debit"]) - flt(entry["credit"])
	for entry in _ledger_entries(ctx, company, start, end):
		bucket(entry["account"])["debit"] += flt(entry["debit"])
		bucket(entry["account"])["credit"] += flt(entry["credit"])
	net = cint(filters.get("show_net_values"))
	rows = []
	for account, v in sorted(accounts.items()):
		closing = v["opening"] + v["debit"] - v["credit"]
		debit, credit = v["debit"], v["credit"]
		if net:
			debit, credit = max(debit - credit, 0), max(credit - debit, 0)
		rows.append({
			"account": account, "opening_debit": round(max(v["opening"], 0), 2),
			"opening_credit": round(max(-v["opening"], 0), 2), "debit": round(debit, 2), "credit": round(credit, 2),
			"closing_debit": round(max(closing, 0), 2), "closing_credit": round(max(-closing, 0), 2),
		})
	columns = [
		col("account", "Account", "Data", 170), col("opening_debit", "Opening (Dr)", "Currency"),
		col("opening_credit", "Opening (Cr)", "Currency"), col("debit", "Debit", "Currency"),
		col("credit", "Credit", "Currency"), col("closing_debit", "Closing (Dr)", "Currency"),
		col("closing_credit", "Closing (Cr)", "Currency"),
	]
	return columns, rows


# Extra reports that users can add from "Manage reports".


@report("Payment Summary", "Payment Entry", "Collections grouped by payment method.", [COMPANY, FROM(), TO()])
def payment_summary(ctx, company, filters):
	start, end = _range(filters)
	totals = {}
	for mode, amount in ctx.db.execute(select(Payment.mode_of_payment, Payment.amount).where(
		Payment.company == company, Payment.posting_date.between(start, end)
	)).all():
		entry = totals.setdefault(mode, {"mode_of_payment": mode, "count": 0, "amount": 0})
		entry["count"] += 1
		entry["amount"] = round(entry["amount"] + flt(amount), 2)
	columns = [col("mode_of_payment", "Payment Method", "Data", 160), col("count", "Payments", "Int", 90), col("amount", "Amount", "Currency")]
	return columns, sorted(totals.values(), key=lambda row: row["amount"], reverse=True)


@report("Daily Sales Summary", "Sales Invoice", "Bills and sales for each day.", [COMPANY, FROM(), TO()])
def daily_sales(ctx, company, filters):
	start, end = _range(filters)
	days = {}
	for inv in _invoices(ctx, company, start, end):
		entry = days.setdefault(str(inv.posting_date), {"posting_date": str(inv.posting_date), "bills": 0, "qty": 0, "grand_total": 0})
		entry["bills"] += 1
		entry["qty"] += inv.total_qty
		entry["grand_total"] = round(entry["grand_total"] + inv.rounded_total, 2)
	columns = [col("posting_date", "Date", "Date"), col("bills", "Bills", "Int", 80), col("qty", "Items Sold", "Float", 100), col("grand_total", "Sales", "Currency")]
	return columns, list(days.values())


@report("Stock Balance", "Item", "Current stock quantity for each stock item.", [
	COMPANY, {"fieldname": "item_group", "label": "Item Group", "fieldtype": "Link", "options": "Item Group"},
])
def stock_balance(ctx, company, filters):
	query = select(Item).where(Item.company == company, Item.is_stock_item.is_(True), Item.disabled.is_(False))
	if filters.get("item_group"):
		query = query.where(Item.item_group == filters["item_group"])
	rows = [
		{"item_code": i.item_code, "item_name": i.item_name, "item_group": i.item_group, "stock_uom": i.stock_uom,
			"qty": i.stock_qty, "rate": i.standard_rate, "amount": round(flt(i.stock_qty) * flt(i.standard_rate), 2)}
		for i in ctx.db.execute(query.order_by(Item.stock_qty)).scalars()
	]
	columns = [
		col("item_code", "Item Code", "Link", 140), col("item_name", "Item Name", "Data", 160), col("item_group", "Item Group"),
		col("stock_uom", "UOM", "Data", 70), col("qty", "Balance Qty", "Float", 100), col("rate", "Rate", "Currency"),
		col("amount", "Stock Value", "Currency"),
	]
	return columns, rows


@report("Expense Register", "Expense", "Store purchases and running costs.", [COMPANY, FROM(), TO()])
def expense_register(ctx, company, filters):
	start, end = _range(filters)
	rows = [
		{"name": e.name, "posting_date": str(e.posting_date), "description": e.description, "amount": e.amount}
		for e in ctx.db.execute(select(Expense).where(
			Expense.company == company, Expense.posting_date.between(start, end)
		).order_by(Expense.posting_date)).scalars()
	]
	columns = [col("name", "Expense", "Data", 140), col("posting_date", "Date", "Date"),
		col("description", "Description", "Data", 240), col("amount", "Amount", "Currency")]
	return columns, rows


# Reports reachable by fixed route keys (sidebar defaults plus the "Expenses" navigation link).
DEFAULT_ROUTES = {route: name for name, route, _, _ in DEFAULT_REPORTS} | {"expenses": "Expense Register"}


def _report_filters(name):
	filters = []
	for definition in REPORTS[name]["filters"]:
		definition = dict(definition)
		if callable(definition.get("default")):
			definition["default"] = definition["default"]()
		filters.append(definition)
	return filters


def _company_or_default(ctx, company):
	company = company or ctx.default_company
	if not company:
		raise AppError("Please configure a default Company before running reports.")
	return ctx.check_company(company)


def configured_reports(ctx, visible_only=False):
	company = ctx.default_company
	if not company:
		return []
	ensure_default_reports(ctx.db, company)
	query = select(CustomReport).where(CustomReport.company == company)
	if visible_only:
		query = query.where(CustomReport.is_visible.is_(True))
	return [
		{
			"report_name": r.report_name, "route_key": r.route_key, "is_visible": int(r.is_visible),
			"is_default": int(r.is_default), "display_order": r.display_order, "accent_color": r.accent_color,
			"report_description": r.report_description or REPORTS.get(r.report_name, {}).get("description"),
		}
		for r in ctx.db.execute(query.order_by(CustomReport.display_order, CustomReport.report_name)).scalars()
	]


@whitelist("frappe.desk.query_report.get_script")
def get_script(ctx, report_name=None):
	if report_name not in REPORTS:
		raise AppError("Report not found.", status_code=404)
	return {"script": "", "filters": _report_filters(report_name), "html_format": ""}


@whitelist("store_management.api.run_my_sales_report", "frappe.desk.query_report.run")
def run_my_sales_report(ctx, report_type=None, filters=None, report_name=None):
	if report_type:
		report_name = DEFAULT_ROUTES.get(report_type)
		if not report_name and ctx.default_company:
			report_name = ctx.db.execute(select(CustomReport.report_name).where(
				CustomReport.company == ctx.default_company, CustomReport.route_key == report_type
			)).scalar()
	if report_name not in REPORTS:
		raise AppError("Unsupported report type")
	filters = parse_json(filters, {}) or {}
	if not isinstance(filters, dict):
		raise AppError("Invalid report filters.")
	company = _company_or_default(ctx, filters.get("company"))
	columns, rows = REPORTS[report_name]["run"](ctx, company, filters)
	return {"columns": columns, "result": rows, "message": None, "chart": None, "report_summary": None}


@whitelist("store_management.api.get_report_sidebar")
def get_report_sidebar(ctx, include_catalog=False):
	reports = configured_reports(ctx)
	result = {"reports": reports}
	if cint(include_catalog):
		configured = {row["report_name"] for row in reports}
		result["catalog"] = [
			{"name": name, "ref_doctype": meta["ref_doctype"], "report_type": "Script Report"}
			for name, meta in sorted(REPORTS.items()) if name not in configured
		]
	return result


def _configured(ctx, report_name):
	row = ctx.db.execute(select(CustomReport).where(
		CustomReport.company == ctx.default_company, CustomReport.report_name == report_name
	)).scalar_one_or_none()
	if not row:
		raise AppError("Report is not configured in My Sales.")
	return row


@whitelist("store_management.api.add_report_to_sidebar", methods=("POST",))
def add_report_to_sidebar(ctx, report_name=None):
	if report_name not in REPORTS:
		raise AppError("Select an available report.")
	company = _company_or_default(ctx, None)
	row = ctx.db.execute(select(CustomReport).where(
		CustomReport.company == company, CustomReport.report_name == report_name
	)).scalar_one_or_none()
	if row:
		row.is_visible = True
	else:
		ctx.db.add(CustomReport(
			company=company, report_name=report_name,
			route_key=re.sub(r"[^a-z0-9]+", "-", report_name.lower()).strip("-"),
			is_visible=True, display_order=100, accent_color="#168650",
		))
	ctx.db.flush()
	return get_report_sidebar(ctx, include_catalog=True)


@whitelist("store_management.api.set_report_sidebar_visibility", methods=("POST",))
def set_report_sidebar_visibility(ctx, report_name=None, visible=0):
	_configured(ctx, report_name).is_visible = bool(cint(visible))
	ctx.db.flush()
	return get_report_sidebar(ctx, include_catalog=True)


@whitelist("store_management.api.remove_report_from_sidebar", methods=("POST",))
def remove_report_from_sidebar(ctx, report_name=None):
	row = _configured(ctx, report_name)
	if row.is_default:
		raise AppError("Default reports can be hidden, but cannot be removed.")
	ctx.db.delete(row)
	ctx.db.flush()
	return get_report_sidebar(ctx, include_catalog=True)
