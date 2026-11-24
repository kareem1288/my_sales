"""Dashboard summary (port of store_management.api._get_dashboard_summary)."""

from datetime import datetime, time

from sqlalchemy import func, select

from ..models import Company, Customer, Expense, Payment, SalesInvoice, SalesInvoiceItem
from ..registry import whitelist
from ..utils import AppError, add_days, first_day, flt, getdate, last_day, today


def _scope(ctx, company):
	if company:
		return [ctx.check_company(company)]
	return list(ctx.companies)


def _current_summaries(ctx, today_date, companies):
	"""Calendar week/month summaries independent of the selected dashboard range."""
	week_start = add_days(today_date, -today_date.weekday())
	week_end = add_days(week_start, 6)
	month_start, month_end = first_day(today_date), last_day(today_date)
	start, end = min(week_start, month_start), max(week_end, month_end)
	db = ctx.db
	sales = db.execute(
		select(SalesInvoice.posting_date, SalesInvoice.grand_total).where(
			SalesInvoice.docstatus == 1, SalesInvoice.company.in_(companies),
			SalesInvoice.posting_date.between(start, end),
		)
	).all()
	expenses = db.execute(
		select(Expense.posting_date, Expense.amount).where(
			Expense.company.in_(companies), Expense.posting_date.between(start, end)
		)
	).all()
	sales_by_day, expenses_by_day, orders_by_day = {}, {}, {}
	for day, total in sales:
		sales_by_day[day] = sales_by_day.get(day, 0) + flt(total)
		orders_by_day[day] = orders_by_day.get(day, 0) + 1
	for day, amount in expenses:
		expenses_by_day[day] = expenses_by_day.get(day, 0) + flt(amount)

	def summarize(start, end):
		rows, day = [], start
		while day <= end:
			sales_value = round(sales_by_day.get(day, 0), 2)
			expense_value = round(expenses_by_day.get(day, 0), 2)
			rows.append({
				"date": str(day), "total": sales_value, "sales": sales_value, "expenses": expense_value,
				"profit": round(sales_value - expense_value, 2), "orders": orders_by_day.get(day, 0),
			})
			day = add_days(day, 1)
		total = round(sum(row["sales"] for row in rows), 2)
		cost = round(sum(row["expenses"] for row in rows), 2)
		orders = sum(row["orders"] for row in rows)
		return {
			"start": str(start), "end": str(end), "rows": rows, "sales": total, "expenses": cost,
			"profit": round(total - cost, 2), "orders": orders,
			"active_days": sum(row["orders"] > 0 for row in rows),
			"average_bill_value": round(total / orders, 2) if orders else 0,
		}

	return {"week": summarize(week_start, week_end), "month": summarize(month_start, month_end)}


def get_summary(ctx, month=None, year=None, company=None, from_date=None, to_date=None):
	db = ctx.db
	today_date = today()
	try:
		selected_month = int(month or today_date.month)
		selected_year = int(year or today_date.year)
	except (TypeError, ValueError):
		raise AppError("Month and year must be valid numbers.")
	if not 1 <= selected_month <= 12 or not 2000 <= selected_year <= 2100:
		raise AppError("Select a valid month and year.")

	if from_date or to_date:
		if not from_date or not to_date:
			raise AppError("Select both From Date and To Date.")
		try:
			period_start, period_end = getdate(from_date), getdate(to_date)
		except ValueError:
			raise AppError("Select valid dates.")
		if period_start > period_end:
			raise AppError("From Date cannot be after To Date.")
		if (period_end - period_start).days > 366:
			raise AppError("Dashboard date range cannot exceed 366 days.")
		selected_month, selected_year = period_start.month, period_start.year
	else:
		period_start = getdate(f"{selected_year:04d}-{selected_month:02d}-01")
		period_end = last_day(period_start)

	companies = _scope(ctx, company)
	submitted = (SalesInvoice.docstatus == 1, SalesInvoice.company.in_(companies))
	in_period = SalesInvoice.posting_date.between(period_start, period_end)

	today_rows = db.execute(
		select(SalesInvoice).where(*submitted, SalesInvoice.posting_date == today_date)
	).scalars().all()
	period_sales = db.execute(
		select(SalesInvoice).where(*submitted, in_period)
		.order_by(SalesInvoice.posting_date.desc(), SalesInvoice.posting_time.desc())
	).scalars().all()
	period_names = [row.name for row in period_sales]
	period_ids = [row.id for row in period_sales]

	def customer_count(start, end, names=None):
		query = select(func.count()).select_from(Customer).where(
			Customer.company.in_(companies),
			Customer.creation >= datetime.combine(start, time.min),
			Customer.creation <= datetime.combine(end, time.max),
		)
		if names is not None:
			query = query.where(Customer.name.in_(names or [""]))
		return db.execute(query).scalar() or 0

	def items_sold(invoice_ids):
		if not invoice_ids:
			return 0
		return flt(db.execute(
			select(func.sum(SalesInvoiceItem.qty)).where(SalesInvoiceItem.invoice_id.in_(invoice_ids))
		).scalar())

	trend_totals = {}
	for row in period_sales:
		trend_totals[str(row.posting_date)] = trend_totals.get(str(row.posting_date), 0) + flt(row.grand_total)
	trend, day = [], period_start
	while day <= period_end:
		trend.append({"date": str(day), "total": round(trend_totals.get(str(day), 0), 2)})
		day = add_days(day, 1)

	item_rows = []
	if period_ids:
		item_rows = db.execute(
			select(SalesInvoiceItem.item_code, SalesInvoiceItem.item_name, SalesInvoiceItem.item_group,
				SalesInvoiceItem.qty, SalesInvoiceItem.amount)
			.where(SalesInvoiceItem.invoice_id.in_(period_ids))
		).all()
	groups, items = {}, {}
	for code, name, group, qty, amount in item_rows:
		groups[group or "Other"] = groups.get(group or "Other", 0) + flt(amount)
		entry = items.setdefault((code, name), {"item_name": name, "sold": 0, "revenue": 0})
		entry["sold"] += flt(qty)
		entry["revenue"] = round(entry["revenue"] + flt(amount), 2)
	categories = [
		{"label": label, "value": round(value, 2)}
		for label, value in sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:5]
	]
	top_items = sorted(items.values(), key=lambda row: row["sold"], reverse=True)[:5]

	period_total = round(sum(flt(row.grand_total) for row in period_sales), 2)
	week_start = max(period_start, add_days(period_end, -6))
	weekly_sales = round(sum(flt(row.grand_total) for row in period_sales if row.posting_date >= week_start), 2)
	weekly_trend = [row for row in trend if getdate(row["date"]) >= week_start]

	payment_totals = {}
	if period_names:
		for mode, amount in db.execute(
			select(Payment.mode_of_payment, func.sum(Payment.amount))
			.where(Payment.invoice.in_(period_names)).group_by(Payment.mode_of_payment)
		).all():
			payment_totals[mode or "Other"] = payment_totals.get(mode or "Other", 0) + flt(amount)
	payment_methods = [
		{"label": label, "value": round(value, 2)}
		for label, value in sorted(payment_totals.items(), key=lambda kv: kv[1], reverse=True)
	]

	hour_totals, store_totals = {}, {}
	for row in period_sales:
		hour = int(str(row.posting_time or "0:00").split(":", 1)[0])
		hour_totals[hour] = hour_totals.get(hour, 0) + flt(row.grand_total)
		store_totals[row.company] = store_totals.get(row.company, 0) + flt(row.grand_total)
	sales_by_hour = [{"hour": hour, "value": round(hour_totals.get(hour, 0), 2)} for hour in range(6, 22)]
	sales_by_store = [
		{"label": name, "value": round(value, 2)}
		for name, value in sorted(store_totals.items(), key=lambda kv: kv[1], reverse=True)
	]

	status_counts = dict(db.execute(
		select(SalesInvoice.docstatus, func.count()).where(SalesInvoice.company.in_(companies), in_period)
		.group_by(SalesInvoice.docstatus)
	).all())
	period_expenses = flt(db.execute(
		select(func.sum(Expense.amount)).where(
			Expense.company.in_(companies), Expense.posting_date.between(period_start, period_end)
		)
	).scalar())

	period_customer_names = list({row.customer for row in period_sales if row.customer})
	new_period_customers = customer_count(period_start, period_end, period_customer_names)
	current = _current_summaries(ctx, today_date, companies)

	recent_bills = [
		{
			"name": row.name, "customer": row.customer, "company": row.company, "grand_total": row.grand_total,
			"posting_date": str(row.posting_date), "posting_time": row.posting_time, "total_qty": row.total_qty,
		}
		for row in period_sales[:5]
	]
	stores = ctx.companies if not ctx.user.is_system_admin else list(
		db.execute(select(Company.name).order_by(Company.name)).scalars()
	)

	return {
		"today_sales": round(sum(flt(row.grand_total) for row in today_rows), 2),
		"today_bills": len(today_rows),
		"today_customers": customer_count(today_date, today_date),
		"today_items_sold": items_sold([row.id for row in today_rows]),
		"month_sales": period_total,
		"period_sales": period_total,
		"weekly_sales": weekly_sales,
		"weekly_trend": weekly_trend,
		"period_bills": len(period_sales),
		"period_customers": customer_count(period_start, period_end),
		"active_customers": len(period_customer_names),
		"new_customers": new_period_customers,
		"returning_customers": max(0, len(period_customer_names) - new_period_customers),
		"period_items_sold": items_sold(period_ids),
		"average_bill_value": round(period_total / len(period_sales), 2) if period_sales else 0,
		"daily_target": 2000,
		"monthly_target": max(5000, round(period_total * 1.25, -2)) if period_total else 5000,
		"period": {
			"month": selected_month, "year": selected_year, "start": str(period_start),
			"end": str(period_end), "company": company or "",
		},
		"stores": stores,
		"recent_bills": recent_bills,
		"trend": trend,
		"categories": categories,
		"top_items": top_items,
		"payment_methods": payment_methods,
		"sales_by_hour": sales_by_hour,
		"sales_by_store": sales_by_store,
		"period_expenses": period_expenses,
		"order_status": {
			"completed": status_counts.get(1, 0), "pending": status_counts.get(0, 0),
			"cancelled": status_counts.get(2, 0),
		},
		"monthly_overview": current["month"]["rows"],
		"current_summaries": current,
	}


@whitelist("store_management.api.get_dashboard_summary")
def get_dashboard_summary(ctx, month=None, year=None, company=None, from_date=None, to_date=None):
	return get_summary(ctx, month=month, year=year, company=company, from_date=from_date, to_date=to_date)
