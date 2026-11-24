"""Global reference data and per-store defaults (replaces after_migrate and ERPNext fixtures)."""

import re

from sqlalchemy import select

from .models import (
	Company, CustomReport, Customer, CustomerGroup, ItemGroup, RoleProfile, UOM, User, UserCompany,
)
from .utils import AppError

COUNTRIES = ["India", "United States", "United Kingdom", "United Arab Emirates"]
CURRENCIES = ["INR", "USD", "GBP", "AED"]
LANGUAGES = ["en", "hi", "te"]
TERRITORIES = ["All Territories", "India", "Rest Of The World"]
UOMS = ["Nos", "Kg", "Gram", "Litre", "Ml", "Box", "Pack", "Dozen", "Meter", "Pair"]
ROLE_PROFILES = {
	"Store Manager": "Store Manager,Cashier,Accounts",
	"Cashier": "Cashier",
	"Accountant": "Accounts,Cashier",
}
WALK_IN_CUSTOMER = "Walk-in Customer"
ROOT_ITEM_GROUP = "All Item Groups"
ROOT_CUSTOMER_GROUP = "All Customer Groups"

# (report_name, route_key, display_order, accent)
DEFAULT_REPORTS = [
	("Sales Register", "sales", 10, "#168650"),
	("Item-wise Sales Register", "items", 20, "#2878c8"),
	("Customer Ledger Summary", "customers", 30, "#7b5cc7"),
	("Accounts Receivable", "open-bills", 40, "#d27a2d"),
	("Profit and Loss Statement", "profit-and-loss", 50, "#e45756"),
	("General Ledger", "general-ledger", 60, "#0f9d91"),
	("Trial Balance", "trial-balance", 70, "#7c5ce5"),
]


def ensure_global_data(db):
	for name in UOMS:
		if not db.execute(select(UOM).where(UOM.name == name)).scalar_one_or_none():
			db.add(UOM(name=name, uom_name=name))
	for name, roles in ROLE_PROFILES.items():
		if not db.execute(select(RoleProfile).where(RoleProfile.name == name)).scalar_one_or_none():
			db.add(RoleProfile(name=name, role_profile=name, roles=roles))
	db.flush()


def unique_abbreviation(db, store_name):
	words = re.findall(r"[A-Za-z0-9]+", store_name or "")
	base = ("".join(word[0] for word in words[:5]).upper() or "STORE")[:5]
	candidate, index = base, 1
	while db.execute(select(Company).where(Company.abbr == candidate)).scalar_one_or_none():
		suffix = str(index)
		candidate = f"{base[: 5 - len(suffix)]}{suffix}"
		index += 1
	return candidate


def create_company(db, company_name, abbr=None, default_currency="INR", country="India", language="en", **extra):
	company_name = (company_name or "").strip()
	if not company_name:
		raise AppError("Company Name is required.")
	if db.execute(select(Company).where(Company.name == company_name)).scalar_one_or_none():
		raise AppError("A company already exists with this store name.")
	abbr = (abbr or "").strip().upper() or unique_abbreviation(db, company_name)
	if db.execute(select(Company).where(Company.abbr == abbr)).scalar_one_or_none():
		raise AppError(f"Abbreviation {abbr} is already used by another company.")
	company = Company(
		name=company_name, company_name=company_name, abbr=abbr, default_currency=default_currency or "INR",
		country=country or "India", custom_application_language=language or "en", **extra,
	)
	db.add(company)
	db.flush()
	ensure_company_defaults(db, company.name)
	return company


def ensure_company_defaults(db, company):
	"""Seed the groups, walk-in customer and report sidebar every store needs to start billing."""

	def missing(model, name):
		return not db.execute(select(model).where(model.company == company, model.name == name)).scalar_one_or_none()

	if missing(ItemGroup, ROOT_ITEM_GROUP):
		db.add(ItemGroup(company=company, name=ROOT_ITEM_GROUP, item_group_name=ROOT_ITEM_GROUP, is_group=True))
	if missing(ItemGroup, "Products"):
		db.add(ItemGroup(company=company, name="Products", item_group_name="Products", parent_item_group=ROOT_ITEM_GROUP))
	if missing(CustomerGroup, ROOT_CUSTOMER_GROUP):
		db.add(CustomerGroup(company=company, name=ROOT_CUSTOMER_GROUP, customer_group_name=ROOT_CUSTOMER_GROUP, is_group=True))
	for group in ("Individual", "Commercial"):
		if missing(CustomerGroup, group):
			db.add(CustomerGroup(company=company, name=group, customer_group_name=group, parent_customer_group=ROOT_CUSTOMER_GROUP))
	if missing(Customer, WALK_IN_CUSTOMER):
		db.add(Customer(
			company=company, name=WALK_IN_CUSTOMER, customer_name=WALK_IN_CUSTOMER, customer_type="Individual",
			customer_group="Individual", territory="India",
		))
	ensure_default_reports(db, company)
	db.flush()


def ensure_default_reports(db, company):
	existing = {row.report_name for row in db.execute(select(CustomReport).where(CustomReport.company == company)).scalars()}
	for report_name, route_key, order, accent in DEFAULT_REPORTS:
		if report_name not in existing:
			db.add(CustomReport(
				company=company, report_name=report_name, route_key=route_key, is_visible=True,
				is_default=True, display_order=order, accent_color=accent,
			))
	db.flush()


def grant_company(db, user_name, company):
	if not db.get(UserCompany, (user_name, company)):
		db.add(UserCompany(user=user_name, company=company))
		db.flush()


def create_user(db, email, first_name, password_hash=None, company=None, **extra):
	email = (email or "").strip().lower()
	if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
		raise AppError("A user account already exists for this email address.")
	user = User(name=email, email=email, first_name=first_name, password_hash=password_hash, default_company=company, **extra)
	db.add(user)
	db.flush()
	if company:
		grant_company(db, user.name, company)
	return user
