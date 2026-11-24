"""Master data CRUD used by /masters (port of the *_master_record APIs)."""

import re

from sqlalchemy import Boolean, Date, Float, Integer, delete, func, select

from ..auth import hash_password
from ..models import (
	Company, Customer, CustomerGroup, Item, ItemGroup, ItemTaxTemplate, RoleProfile, SalesInvoice,
	SalesInvoiceItem, UOM, User, UserCompany, CustomReport, Payment, Expense,
)
from ..registry import whitelist
from ..setup import (
	COUNTRIES, CURRENCIES, LANGUAGES, ROOT_CUSTOMER_GROUP, ROOT_ITEM_GROUP, TERRITORIES, WALK_IN_CUSTOMER,
	create_company, create_user, grant_company,
)
from ..utils import AppError, NotFoundError, PermissionError_, cint, getdate, next_name, row_to_dict

# doctype -> (model, scope, editable fields)
#   scope "company": rows belong to the user's current store
#   scope "global":  shared reference data (read-only here)
MASTERS = {
	"Item": (Item, "company", {
		"item_code", "item_name", "item_group", "stock_uom", "is_stock_item", "opening_stock", "standard_rate",
		"image", "barcode", "description", "disabled",
	}),
	"Item Group": (ItemGroup, "company", {"item_group_name", "parent_item_group", "is_group", "image"}),
	"Customer": (Customer, "company", {
		"customer_name", "customer_type", "customer_group", "territory", "tax_id", "mobile_no", "disabled",
	}),
	"Customer Group": (CustomerGroup, "company", {"customer_group_name", "parent_customer_group", "is_group"}),
	"Item Tax Template": (ItemTaxTemplate, "multi", {"title", "company", "disabled"}),
	"Expense": (Expense, "company", {"posting_date", "description", "amount"}),
	"Company": (Company, "special", {"company_name", "abbr", "default_currency", "country", "custom_application_language"}),
	"User": (User, "special", {"email", "first_name", "last_name", "mobile_no", "role_profiles", "enabled", "language"}),
	"Role Profile": (RoleProfile, "global", set()),
	"UOM": (UOM, "global", set()),
}
STATIC_LINKS = {
	"Country": COUNTRIES, "Currency": CURRENCIES, "Language": LANGUAGES, "Territory": TERRITORIES,
}
CUSTOMER_TYPES = {"Company", "Individual", "Partnership"}
# Customers can be added by any signed-in user (e.g. a cashier at the till); the rest needs a manager.
CASHIER_WRITABLE = {"Customer"}


def _master(doctype):
	if doctype not in MASTERS:
		raise PermissionError_(f"{doctype} is not available in My Sales")
	return MASTERS[doctype]


def _company(ctx):
	company = ctx.default_company
	if not company:
		raise AppError("Set up a store first.")
	return company


def _query(ctx, doctype):
	model, scope, _ = _master(doctype)
	query = select(model)
	if scope == "company":
		query = query.where(model.company == _company(ctx))
	elif scope == "multi":
		query = query.where(model.company.in_(ctx.companies))
	elif doctype == "Company":
		query = query.where(model.name.in_(ctx.companies))
	elif doctype == "User":
		members = select(UserCompany.user).where(UserCompany.company.in_(ctx.companies))
		query = query.where(model.name.in_(members) | (model.name == ctx.user_name))
	return query.order_by(model.name)


def _get(ctx, doctype, name):
	model = _master(doctype)[0]
	row = ctx.db.execute(_query(ctx, doctype).where(model.name == name)).scalars().first()
	if not row:
		raise NotFoundError(f"{doctype} {name} not found")
	return row


def _as_dict(doctype, row):
	data = row_to_dict(row)
	data["doctype"] = doctype
	if doctype == "User":
		data["full_name"] = row.full_name
		data.pop("is_system_admin", None)
	return data


def _clean(model, allowed, values):
	"""Keep editable fields and coerce them to their column types."""
	cleaned = {}
	for key, value in values.items():
		if key not in allowed:
			continue
		column = model.__table__.columns.get(key)
		if column is None:
			cleaned[key] = value
			continue
		if isinstance(column.type, Boolean):
			value = bool(cint(value))
		elif isinstance(column.type, Float):
			try:
				value = float(value or 0)
			except (TypeError, ValueError):
				raise AppError(f"{key.replace('_', ' ').title()} must be a number.")
		elif isinstance(column.type, Integer):
			value = cint(value)
		elif isinstance(column.type, Date):
			try:
				value = getdate(value) if value else None
			except ValueError:
				raise AppError(f"{key.replace('_', ' ').title()} must be a valid date.")
		else:
			value = (str(value).strip() if value is not None else None) or None
		cleaned[key] = value
	return cleaned


def _exists(ctx, model, company, name):
	return bool(name) and ctx.db.execute(
		select(model.id).where(model.company == company, model.name == name)
	).first() is not None


def _validate(ctx, doctype, values, company, row=None):
	db = ctx.db
	get = lambda key: values.get(key, getattr(row, key, None) if row else None)  # noqa: E731
	if doctype == "Item":
		if not get("item_code") or not get("item_name"):
			raise AppError("Item Code and Item Name are required.")
		if not _exists(ctx, ItemGroup, company, get("item_group")):
			raise AppError(f"Item Group {get('item_group')} does not exist.")
		if not db.execute(select(UOM.id).where(UOM.name == (get("stock_uom") or "Nos"))).first():
			raise AppError(f"UOM {get('stock_uom')} does not exist.")
		if (get("standard_rate") or 0) < 0:
			raise AppError("Standard Rate cannot be negative.")
	elif doctype == "Item Group":
		parent = get("parent_item_group")
		if parent and not _exists(ctx, ItemGroup, company, parent):
			raise AppError(f"Parent Item Group {parent} does not exist.")
		if row and parent == row.name:
			raise AppError("An Item Group cannot be its own parent.")
	elif doctype == "Customer":
		if not get("customer_name"):
			raise AppError("Customer Name is required.")
		if get("customer_type") and get("customer_type") not in CUSTOMER_TYPES:
			raise AppError("Select a valid Customer Type.")
		group = get("customer_group")
		if group and not _exists(ctx, CustomerGroup, company, group):
			raise AppError(f"Customer Group {group} does not exist.")
		if get("territory") and get("territory") not in TERRITORIES:
			raise AppError("Select a valid Territory.")
	elif doctype == "Customer Group":
		parent = get("parent_customer_group")
		if parent and not _exists(ctx, CustomerGroup, company, parent):
			raise AppError(f"Parent Customer Group {parent} does not exist.")
		if row and parent == row.name:
			raise AppError("A Customer Group cannot be its own parent.")
	elif doctype == "Expense":
		if not get("posting_date") or not get("description"):
			raise AppError("Date and Description are required.")
		if (get("amount") or 0) <= 0:
			raise AppError("Amount must be greater than zero.")
	elif doctype == "Item Tax Template":
		if not get("title"):
			raise AppError("Title is required.")
		ctx.check_company(get("company"))
	elif doctype == "Company":
		if values.get("default_currency") and values["default_currency"] not in CURRENCIES:
			raise AppError("Select a valid currency.")
		if values.get("country") and values["country"] not in COUNTRIES:
			raise AppError("Select a valid country.")
		if values.get("custom_application_language") and values["custom_application_language"] not in LANGUAGES:
			raise AppError("Select a valid application language.")


def _unique_name(ctx, model, company, base):
	name, index = base, 1
	while _exists(ctx, model, company, name):
		name = f"{base} - {index}"
		index += 1
	return name


def _role_profiles(ctx, value):
	if value is None:
		return None
	names = [p.strip() for p in str(value).split(",") if p.strip()]
	known = set(ctx.db.execute(select(RoleProfile.name).where(RoleProfile.name.in_(names))).scalars())
	unknown = [n for n in names if n not in known]
	if unknown:
		raise AppError(f"Role Profile {unknown[0]} does not exist.")
	return ",".join(dict.fromkeys(names))


def _check_write(ctx, doctype):
	ctx.require_login()
	if doctype == "Expense" and "Accounts" in ctx.roles:
		return
	if doctype not in CASHIER_WRITABLE:
		ctx.require_manager()
	if MASTERS[doctype][1] == "global":
		raise PermissionError_(f"{doctype} records are managed by the system.")


@whitelist("store_management.api.get_master_records")
def get_master_records(ctx, doctype=None):
	return [_as_dict(doctype, row) for row in ctx.db.execute(_query(ctx, doctype)).scalars()]


@whitelist("store_management.api.get_master_link_records")
def get_master_link_records(ctx, doctype=None):
	if doctype in STATIC_LINKS:
		return [{"name": name} for name in STATIC_LINKS[doctype]]
	model = _master(doctype)[0]
	return [{"name": name} for name in ctx.db.execute(_query(ctx, doctype).with_only_columns(model.name)).scalars()]


@whitelist("store_management.api.get_master_record")
def get_master_record(ctx, doctype=None, name=None):
	return _as_dict(doctype, _get(ctx, doctype, name))


@whitelist("store_management.api.create_master_record", methods=("POST",))
def create_master_record(ctx, doctype=None, **kwargs):
	model, scope, allowed = _master(doctype)
	_check_write(ctx, doctype)
	db = ctx.db
	password = (kwargs.get("new_password") or "").strip()
	values = _clean(model, allowed, kwargs)

	if doctype == "Company":
		_validate(ctx, doctype, values, None)
		company = create_company(
			db, values.get("company_name"), abbr=values.get("abbr"), default_currency=values.get("default_currency"),
			country=values.get("country"), language=values.get("custom_application_language"),
		)
		grant_company(db, ctx.user_name, company.name)
		return {"name": company.name}

	if doctype == "User":
		email = (values.get("email") or "").lower()
		if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
			raise AppError("Enter a valid email address.")
		if not values.get("first_name"):
			raise AppError("First Name is required.")
		if password:
			_check_password_strength(password)
		user = create_user(
			db, email, values["first_name"], password_hash=hash_password(password) if password else None,
			company=_company(ctx), last_name=values.get("last_name"), mobile_no=values.get("mobile_no"),
			role_profiles=_role_profiles(ctx, kwargs.get("role_profiles")) or "Cashier",
			language=values.get("language") or "en",
		)
		return {"name": user.name}

	company = values.get("company") if scope == "multi" else _company(ctx)
	if scope == "multi":
		values["company"] = company
	_validate(ctx, doctype, values, company)
	if doctype == "Item":
		if _exists(ctx, Item, company, values["item_code"]):
			raise AppError(f"Item {values['item_code']} already exists.")
		name = values["item_code"]
		values.setdefault("stock_uom", "Nos")
		values["stock_qty"] = values.get("opening_stock") or 0
	elif doctype == "Item Group":
		name = values.get("item_group_name")
		if not name:
			raise AppError("Item Group Name is required.")
		if _exists(ctx, ItemGroup, company, name):
			raise AppError(f"Item Group {name} already exists.")
		values.setdefault("parent_item_group", ROOT_ITEM_GROUP)
	elif doctype == "Customer Group":
		name = values.get("customer_group_name")
		if not name:
			raise AppError("Customer Group Name is required.")
		if _exists(ctx, CustomerGroup, company, name):
			raise AppError(f"Customer Group {name} already exists.")
		values.setdefault("parent_customer_group", ROOT_CUSTOMER_GROUP)
	elif doctype == "Customer":
		name = _unique_name(ctx, Customer, company, values["customer_name"])
	elif doctype == "Expense":
		name = next_name(db, f"EXP-{values['posting_date'].year}")
		values["owner"] = ctx.user_name
	elif doctype == "Item Tax Template":
		abbr = db.execute(select(Company.abbr).where(Company.name == company)).scalar()
		name = f"{values['title']} - {abbr}"
		if _exists(ctx, ItemTaxTemplate, company, name):
			raise AppError(f"Item Tax Template {name} already exists.")
	values.pop("company", None)
	row = model(company=company, name=name, **values)
	db.add(row)
	db.flush()
	return {"name": row.name}


@whitelist("store_management.api.update_master_record", methods=("POST",))
def update_master_record(ctx, doctype=None, name=None, **kwargs):
	model, scope, allowed = _master(doctype)
	_check_write(ctx, doctype)
	row = _get(ctx, doctype, name)
	password = (kwargs.get("new_password") or "").strip()
	values = _clean(model, allowed - {"item_code", "email"}, kwargs)

	if doctype == "User":
		if "role_profiles" in kwargs:
			profiles = _role_profiles(ctx, kwargs.get("role_profiles"))
			if profiles is not None and row.name == ctx.user_name and "Store Manager" not in profiles.split(",") \
					and not ctx.user.is_system_admin:
				raise AppError("You cannot remove your own Store Manager access.")
			values["role_profiles"] = profiles or row.role_profiles
		if "enabled" in values and not values["enabled"] and row.name == ctx.user_name:
			raise AppError("You cannot disable your own account.")
		if password:
			_check_password_strength(password)
			row.password_hash = hash_password(password)
			row.session_version += 1
		if "enabled" in values and not values["enabled"]:
			row.session_version += 1
	elif doctype == "Company":
		_validate(ctx, doctype, values, row.name)
		if values.get("abbr") and values["abbr"] != row.abbr:
			values["abbr"] = values["abbr"].upper()
			if ctx.db.execute(select(Company.id).where(Company.abbr == values["abbr"])).first():
				raise AppError(f"Abbreviation {values['abbr']} is already used by another company.")
		language = values.get("custom_application_language")
		if language:
			members = select(UserCompany.user).where(UserCompany.company == row.name)
			for user in ctx.db.execute(select(User).where(User.name.in_(members))).scalars():
				user.language = language
	else:
		company = row.company
		if scope == "multi":
			values.pop("company", None)
		_validate(ctx, doctype, values, company, row)
		if doctype == "Item" and "opening_stock" in values:
			row.stock_qty = (row.stock_qty or 0) + values["opening_stock"] - (row.opening_stock or 0)
		if doctype == "Customer" and row.name == WALK_IN_CUSTOMER and values.get("disabled"):
			raise AppError("The walk-in customer cannot be disabled.")

	for key, value in values.items():
		setattr(row, key, value)
	ctx.db.flush()
	return {"name": row.name}


def _in_use(ctx, query, message):
	if ctx.db.execute(query.limit(1)).first():
		raise AppError(message)


@whitelist("store_management.api.delete_master_record", methods=("POST",))
def delete_master_record(ctx, doctype=None, name=None):
	_check_write(ctx, doctype)
	row = _get(ctx, doctype, name)
	db = ctx.db
	if doctype == "Item":
		_in_use(ctx, select(SalesInvoiceItem.id).join(SalesInvoice).where(
			SalesInvoice.company == row.company, SalesInvoiceItem.item_code == row.name),
			f"Cannot delete Item {name} because it is used in bills. Disable it instead.")
	elif doctype == "Customer":
		if row.name == WALK_IN_CUSTOMER:
			raise AppError("The walk-in customer cannot be deleted.")
		_in_use(ctx, select(SalesInvoice.id).where(SalesInvoice.company == row.company, SalesInvoice.customer == row.name),
			f"Cannot delete Customer {name} because it has bills. Disable it instead.")
	elif doctype == "Item Group":
		if row.name == ROOT_ITEM_GROUP:
			raise AppError("The root item group cannot be deleted.")
		_in_use(ctx, select(Item.id).where(Item.company == row.company, Item.item_group == row.name),
			f"Cannot delete Item Group {name} because items belong to it.")
		_in_use(ctx, select(ItemGroup.id).where(ItemGroup.company == row.company, ItemGroup.parent_item_group == row.name),
			f"Cannot delete Item Group {name} because it has child groups.")
	elif doctype == "Customer Group":
		if row.name == ROOT_CUSTOMER_GROUP:
			raise AppError("The root customer group cannot be deleted.")
		_in_use(ctx, select(Customer.id).where(Customer.company == row.company, Customer.customer_group == row.name),
			f"Cannot delete Customer Group {name} because customers belong to it.")
		_in_use(ctx, select(CustomerGroup.id).where(CustomerGroup.company == row.company, CustomerGroup.parent_customer_group == row.name),
			f"Cannot delete Customer Group {name} because it has child groups.")
	elif doctype == "User":
		if row.name == ctx.user_name:
			raise AppError("You cannot delete your own account.")
		if row.is_system_admin:
			raise AppError("System administrators cannot be deleted here.")
		shared = db.execute(select(func.count()).select_from(UserCompany).where(UserCompany.user == row.name)).scalar()
		if shared > 1 and not ctx.user.is_system_admin:
			raise AppError("This user belongs to other stores and cannot be deleted here.")
		db.execute(delete(UserCompany).where(UserCompany.user == row.name))
	elif doctype == "Company":
		if len(ctx.companies) <= 1:
			raise AppError("You cannot delete your only store.")
		_in_use(ctx, select(SalesInvoice.id).where(SalesInvoice.company == row.name),
			f"Cannot delete Company {name} because it has bills.")
		for model in (Item, ItemGroup, Customer, CustomerGroup, ItemTaxTemplate, CustomReport, Payment, Expense):
			db.execute(delete(model).where(model.company == row.name))
		db.execute(delete(UserCompany).where(UserCompany.company == row.name))
		db.query(User).filter(User.default_company == row.name).update({User.default_company: None})
	db.delete(row)
	db.flush()
	return {"success": True}


def _check_password_strength(password):
	if len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"[0-9\W]", password):
		raise AppError(
			"Password must contain at least 8 characters, one uppercase letter, and one number or special character."
		)
