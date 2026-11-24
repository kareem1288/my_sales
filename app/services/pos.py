"""POS: bootstrap data, barcode lookup, billing, and UPI payment requests."""

import base64
import io
import math
import secrets

from sqlalchemy import or_, select

from ..config import get_settings
from ..models import Customer, Item, Payment, SalesInvoice, SalesInvoiceItem
from ..registry import whitelist
from ..setup import WALK_IN_CUSTOMER, ensure_company_defaults
from ..utils import AppError, flt, money_round, next_name, now, parse_json
from .dashboard import get_summary

CATEGORY_COLORS = ["#1F9D55", "#0F766E", "#2563EB", "#C2410C", "#BE185D", "#7C3AED", "#CA8A04", "#0891B2"]
SPLIT_METHODS = {"Cash", "Card"}
PAYMENT_METHODS = {"Cash", "UPI", "Card", "Split"}


def item_payload(item):
	return {
		"name": item.name, "item_name": item.item_name, "item_group": item.item_group,
		"standard_rate": item.standard_rate, "image": item.image or "", "stock_uom": item.stock_uom,
		"description": item.description or "", "barcode": item.barcode or "",
	}


def _sales_items(ctx, company):
	return ctx.db.execute(
		select(Item).where(Item.company == company, Item.disabled.is_(False), Item.is_sales_item.is_(True))
		.order_by(Item.item_group, Item.item_name)
	).scalars().all()


def default_customer(ctx, company):
	customer = ctx.db.execute(
		select(Customer).where(Customer.company == company, Customer.name == WALK_IN_CUSTOMER)
	).scalar_one_or_none()
	if not customer:
		ensure_company_defaults(ctx.db, company)
	return WALK_IN_CUSTOMER


def get_bootstrap(ctx):
	company = ctx.default_company
	items = _sales_items(ctx, company) if company else []
	category_names = []
	for item in items:
		if item.item_group and item.item_group not in category_names:
			category_names.append(item.item_group)
	return {
		"shop_name": company or "Retail POS",
		"company": company,
		"default_customer": default_customer(ctx, company) if company else None,
		"categories": [
			{"name": name, "color": CATEGORY_COLORS[index % len(CATEGORY_COLORS)]}
			for index, name in enumerate(category_names)
		],
		"items": [item_payload(item) for item in items],
		"summary": get_summary(ctx),
	}


@whitelist("store_management.api.get_pos_bootstrap")
def get_pos_bootstrap(ctx):
	return get_bootstrap(ctx)


@whitelist("store_management.api.get_pos_categories")
def get_pos_categories(ctx):
	return get_bootstrap(ctx)["categories"]


@whitelist("store_management.api.get_pos_items")
def get_pos_items(ctx):
	return get_bootstrap(ctx)["items"]


@whitelist("store_management.api.get_pos_items_by_barcode")
def get_pos_items_by_barcode(ctx, query=None):
	query = (query or "").strip()
	company = ctx.default_company
	if not query or not company:
		return []
	base = select(Item).where(Item.company == company, Item.disabled.is_(False))
	rows = ctx.db.execute(base.where(Item.barcode == query).order_by(Item.item_name)).scalars().all()
	if not rows:
		like = f"%{query}%"
		rows = ctx.db.execute(
			base.where(or_(Item.barcode.like(like), Item.item_code == query)).order_by(Item.item_name)
		).scalars().all()
	return [item_payload(item) for item in rows]


def _parse_items(items):
	items = parse_json(items, [])
	if not isinstance(items, list):
		return []
	parsed = []
	for row in items:
		if not isinstance(row, dict):
			continue
		item_code = row.get("item_code") or row.get("name")
		qty = flt(row.get("qty") or row.get("quantity"))
		rate = flt(row.get("rate") or row.get("standard_rate"))
		if item_code and math.isfinite(qty) and math.isfinite(rate) and qty > 0 and rate > 0:
			parsed.append({"item_code": str(item_code), "qty": qty, "rate": money_round(rate, 2)})
	return parsed


def _resolve_customer(ctx, company, customer=None, customer_phone=None):
	base = select(Customer).where(Customer.company == company, Customer.disabled.is_(False))
	if customer:
		for condition in (Customer.name == customer, Customer.customer_name == customer):
			match = ctx.db.execute(base.where(condition)).scalars().first()
			if match:
				return match
	if customer_phone:
		match = ctx.db.execute(base.where(Customer.mobile_no == customer_phone)).scalars().first()
		if match:
			return match
	default_customer(ctx, company)
	return ctx.db.execute(base.where(Customer.name == WALK_IN_CUSTOMER)).scalar_one()


def _validate_split_payments(parts, total):
	parts = parse_json(parts, None)
	if not isinstance(parts, list) or len(parts) != 2:
		raise AppError("Split payment requires Cash and Card amounts.")
	parsed = []
	for part in parts:
		if not isinstance(part, dict) or part.get("method") not in SPLIT_METHODS:
			raise AppError("Select valid split payment methods.")
		try:
			amount = float(part.get("amount", 0))
		except (TypeError, ValueError):
			raise AppError("Enter valid split payment amounts.")
		if not math.isfinite(amount) or round(amount, 2) <= 0:
			raise AppError("Each split payment amount must be greater than zero.")
		parsed.append({"method": part["method"], "amount": round(amount, 2)})
	if len({p["method"] for p in parsed}) != 2 or abs(sum(p["amount"] for p in parsed) - flt(total)) > 0.005:
		raise AppError("Cash and Card amounts must add up to the invoice total.")
	return parsed


@whitelist("store_management.api.create_pos_bill", methods=("POST",))
def create_pos_bill(
	ctx, customer=None, customer_phone=None, payment_method="Cash", payment_reference=None, items=None,
	additional_discount_amount=0, notes=None, split_payments=None,
):
	db = ctx.db
	company = ctx.default_company
	if not company:
		raise AppError("Please set up a store before billing.")
	payment_method = payment_method or "Cash"
	if payment_method not in PAYMENT_METHODS:
		raise AppError("Select a valid payment method.")
	cart = _parse_items(items)
	if not cart:
		raise AppError("No items in cart")

	codes = {row["item_code"] for row in cart}
	stock = {
		item.name: item for item in db.execute(
			select(Item).where(Item.company == company, Item.name.in_(codes), Item.disabled.is_(False))
		).scalars()
	}
	missing = sorted(codes - stock.keys())
	if missing:
		raise AppError(f"Item {missing[0]} is not available for sale.")

	customer_doc = _resolve_customer(ctx, company, customer, customer_phone)
	current = now()
	invoice = SalesInvoice(
		name=next_name(db, f"SINV-{current.year}"), company=company, customer=customer_doc.name,
		customer_name=customer_doc.customer_name, posting_date=current.date(),
		posting_time=current.strftime("%H:%M:%S"), owner=ctx.user_name, docstatus=1,
		contact_mobile=(customer_phone or customer_doc.mobile_no or None),
	)
	net_total = total_qty = 0
	for idx, row in enumerate(cart, start=1):
		item = stock[row["item_code"]]
		amount = money_round(row["qty"] * row["rate"], 2)
		invoice.items.append(SalesInvoiceItem(
			idx=idx, item_code=item.item_code, item_name=item.item_name, item_group=item.item_group,
			qty=row["qty"], rate=row["rate"], amount=amount,
		))
		if item.is_stock_item:
			item.stock_qty = flt(item.stock_qty) - row["qty"]
		net_total += amount
		total_qty += row["qty"]

	discount = money_round(flt(additional_discount_amount), 2)
	if discount < 0 or discount > net_total:
		raise AppError("Discount must be between zero and the bill total.")
	grand_total = money_round(net_total - discount, 2)
	rounded_total = money_round(grand_total, 0)
	invoice.net_total = money_round(net_total, 2)
	invoice.total_qty = total_qty
	invoice.discount_amount = discount
	invoice.grand_total = grand_total
	invoice.rounded_total = rounded_total
	invoice.rounding_adjustment = money_round(rounded_total - grand_total, 2)

	remarks = [f"Payment Mode: {payment_method}"]
	if notes:
		remarks.append(str(notes)[:1000])
	if customer_phone:
		remarks.append(f"Customer Phone: {customer_phone}")
	invoice.remarks = " | ".join(remarks)

	parts = (
		_validate_split_payments(split_payments, rounded_total) if payment_method == "Split"
		else [{"method": payment_method, "amount": rounded_total}]
	)
	db.add(invoice)
	db.flush()

	payments = []
	for part in parts:
		payment = Payment(
			name=next_name(db, f"PAY-{current.year}"), company=company, invoice=invoice.name,
			customer=customer_doc.name, posting_date=invoice.posting_date, mode_of_payment=part["method"],
			amount=part["amount"], reference_no=(payment_reference if len(parts) == 1 else None) or invoice.name,
		)
		db.add(payment)
		payments.append(payment)
	invoice.paid_amount = rounded_total
	invoice.outstanding_amount = 0
	db.flush()

	return {
		"name": invoice.name,
		"customer": invoice.customer,
		"grand_total": invoice.grand_total,
		"rounded_total": invoice.rounded_total,
		"posting_date": str(invoice.posting_date),
		"payment_entry": payments[0].name,
		"payment_entries": [p.name for p in payments],
		"bill_attachment_queued": False,
	}


# ---------------------------------------------------------------------- UPI


def _qr_data_uri(value):
	import segno

	output = io.BytesIO()
	segno.make(value, error="m").save(output, kind="svg", scale=6, border=2, dark="#101512")
	return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")


@whitelist("store_management.api.create_upi_payment", methods=("POST",))
def create_upi_payment(ctx, amount=None):
	settings = get_settings()
	amount = flt(amount, 2)
	if amount <= 0:
		raise AppError("Payment amount must be greater than zero.")
	if not settings.upi_enabled:
		raise AppError("UPI payments are disabled. Set UPI_ENABLED to enable them.")
	company = ctx.default_company
	reference = f"UPI-{secrets.token_hex(8).upper()}"

	if settings.upi_mode != "Live":
		# Deliberately not a upi://pay URI: scanning this QR can never initiate a debit.
		payment_uri = f"mysales-test://payment/{reference}?amount=0&display_amount={amount:.2f}"
		return {
			"mode": "Testing", "chargeable": False, "amount": amount, "transaction_id": reference,
			"payment_uri": payment_uri, "qr_code": _qr_data_uri(payment_uri),
			"upi_id": settings.upi_id or "Test payment", "company": company,
			"message": "Test QR generated. No amount will be deducted.",
		}

	if not settings.upi_payment_api_url.startswith("https://"):
		raise AppError("Configure a valid HTTPS UPI_PAYMENT_API_URL for Live mode.")
	if not settings.upi_api_key or not settings.upi_api_secret:
		raise AppError("Configure UPI_API_KEY and UPI_API_SECRET for Live mode.")

	import requests

	payload = {
		"amount": amount, "currency": "INR", "reference": reference, "upi_id": settings.upi_id,
		"company": company, "redirect_url": settings.upi_redirect_to,
	}
	try:
		response = requests.post(
			settings.upi_payment_api_url, json=payload, timeout=20,
			headers={"X-API-Key": settings.upi_api_key, "X-API-Secret": settings.upi_api_secret, "Accept": "application/json"},
		)
		response.raise_for_status()
		gateway = response.json()
	except (requests.RequestException, ValueError):
		raise AppError("The UPI gateway could not create a payment request. Please try again.")
	payment_uri = gateway.get("payment_uri") or gateway.get("upi_uri") or gateway.get("payment_url")
	if not payment_uri:
		raise AppError("The UPI gateway response did not contain a payment URI.")
	return {
		"mode": "Live", "chargeable": True, "amount": amount,
		"transaction_id": gateway.get("transaction_id") or gateway.get("id") or reference,
		"payment_uri": payment_uri, "qr_code": gateway.get("qr_code") or _qr_data_uri(payment_uri),
		"upi_id": settings.upi_id, "expires_in": gateway.get("expires_in") or 300,
		"message": "Live payment request created.",
	}


def invoice_for_print(ctx, name):
	invoice = ctx.db.execute(
		select(SalesInvoice).where(SalesInvoice.name == name, SalesInvoice.company.in_(ctx.companies))
	).scalar_one_or_none()
	if not invoice:
		raise AppError("Bill not found.", status_code=404)
	return invoice

