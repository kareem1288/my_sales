"""Command-line tasks.

  python -m app.cli init-db
  python -m app.cli create-admin EMAIL PASSWORD [--name NAME]
  python -m app.cli create-store "Store Name" EMAIL PASSWORD [--name NAME]
  python -m app.cli seed-demo [--days 60]
  python -m app.cli expire-trials
"""

import argparse
import random

from sqlalchemy import select

from .auth import hash_password
from .db import init_db, session_scope
from .models import Customer, Expense, Item, ItemGroup, Payment, SalesInvoice, SalesInvoiceItem, User
from .setup import create_company, create_user, ensure_global_data, grant_company
from .utils import add_days, money_round, next_name, today

DEMO_COMPANY = "Demo Company"
DEMO_ITEMS = [
	("DEMO-RICE-5KG", "Premium Rice 5 kg", "Groceries", 420, "8901000000011"),
	("DEMO-OIL-1L", "Sunflower Oil 1 L", "Groceries", 155, "8901000000028"),
	("DEMO-SUGAR-1KG", "Sugar 1 kg", "Groceries", 52, "8901000000035"),
	("DEMO-FLOUR-5KG", "Wheat Flour 5 kg", "Groceries", 285, "8901000000042"),
	("DEMO-DAL-1KG", "Toor Dal 1 kg", "Groceries", 175, "8901000000059"),
	("DEMO-TEA-250G", "Tea 250 g", "Beverages", 140, "8901000000066"),
	("DEMO-COFFEE-200G", "Coffee 200 g", "Beverages", 260, "8901000000073"),
	("DEMO-SOAP-PACK", "Bath Soap Pack", "Personal Care", 120, "8901000000080"),
	("DEMO-SHAMPOO", "Shampoo 180 ml", "Personal Care", 199, "8901000000097"),
	("DEMO-BISCUIT", "Biscuits", "Snacks", 30, "8901000000103"),
]
DEMO_CUSTOMERS = ["Aarav Retail", "Meera Stores", "Sai Traders", "Lakshmi Mart"]


def cmd_create_admin(args):
	with session_scope() as db:
		ensure_global_data(db)
		user = db.execute(select(User).where(User.email == args.email.lower())).scalar_one_or_none()
		if user:
			user.password_hash = hash_password(args.password)
			user.is_system_admin = True
			user.enabled = True
			user.session_version += 1
			print(f"Updated administrator {user.email}")
		else:
			create_user(db, args.email, args.name, password_hash=hash_password(args.password), is_system_admin=True,
				role_profiles="Store Manager")
			print(f"Created administrator {args.email}")


def cmd_create_store(args):
	with session_scope() as db:
		ensure_global_data(db)
		company = create_company(db, args.store)
		create_user(db, args.email, args.name, password_hash=hash_password(args.password), company=company.name,
			role_profiles="Store Manager")
		print(f"Created store {company.name} with manager {args.email}")


def cmd_seed_demo(args):
	"""Create a demo store with items, customers, paid bills and expenses (safe to re-run)."""
	rng = random.Random(42)
	with session_scope() as db:
		ensure_global_data(db)
		from .models import Company

		if db.execute(select(Company).where(Company.name == DEMO_COMPANY)).scalar_one_or_none():
			print("Demo Company already exists; nothing to do.")
			return
		create_company(db, DEMO_COMPANY, abbr="DEMO", address="12 Market Road, Hyderabad, Telangana 500001",
			phone="+919000000000", tax_id="36ABCDE1234F1Z5")
		user = create_user(db, "demo@example.com", "Demo", password_hash=hash_password("Demo@1234"),
			company=DEMO_COMPANY, last_name="Manager", role_profiles="Store Manager", mobile_no="+919000000000")
		admins = db.execute(select(User).where(User.is_system_admin.is_(True))).scalars().all()
		for admin in admins:
			grant_company(db, admin.name, DEMO_COMPANY)

		for group in {g for _, _, g, _, _ in DEMO_ITEMS}:
			db.add(ItemGroup(company=DEMO_COMPANY, name=group, item_group_name=group, parent_item_group="All Item Groups"))
		items = []
		for code, name, group, rate, barcode in DEMO_ITEMS:
			item = Item(company=DEMO_COMPANY, name=code, item_code=code, item_name=name, item_group=group,
				standard_rate=rate, barcode=barcode, opening_stock=100, stock_qty=100)
			db.add(item)
			items.append(item)
		customers = ["Walk-in Customer"]
		for name in DEMO_CUSTOMERS:
			db.add(Customer(company=DEMO_COMPANY, name=name, customer_name=name, customer_group="Commercial",
				customer_type="Company", territory="India"))
			customers.append(name)
		db.flush()

		modes = ["Cash", "UPI", "Card"]
		start = add_days(today(), -args.days)
		bills = 0
		for offset in range(args.days + 1):
			day = add_days(start, offset)
			for _ in range(rng.randint(2, 6)):
				chosen = rng.sample(items, rng.randint(1, 4))
				invoice = SalesInvoice(
					name=next_name(db, f"SINV-{day.year}"), company=DEMO_COMPANY, customer=(c := rng.choice(customers)),
					customer_name=c, posting_date=day, posting_time=f"{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:00",
					owner=user.name, docstatus=1, remarks="Demo data",
				)
				net = qty_total = 0
				for idx, item in enumerate(chosen, start=1):
					qty = rng.randint(1, 4)
					amount = qty * item.standard_rate
					invoice.items.append(SalesInvoiceItem(idx=idx, item_code=item.item_code, item_name=item.item_name,
						item_group=item.item_group, qty=qty, rate=item.standard_rate, amount=amount))
					item.stock_qty -= qty
					net += amount
					qty_total += qty
				invoice.net_total = invoice.grand_total = invoice.rounded_total = invoice.paid_amount = money_round(net, 2)
				invoice.total_qty = qty_total
				db.add(invoice)
				db.flush()
				mode = rng.choice(modes)
				invoice.remarks = f"Payment Mode: {mode} | Demo data"
				db.add(Payment(name=next_name(db, f"PAY-{day.year}"), company=DEMO_COMPANY, invoice=invoice.name,
					customer=invoice.customer, posting_date=day, mode_of_payment=mode, amount=invoice.rounded_total,
					reference_no=invoice.name))
				bills += 1
			if offset % 7 == 0:
				db.add(Expense(name=next_name(db, f"EXP-{day.year}"), company=DEMO_COMPANY, posting_date=day,
					description="Stock purchase", amount=rng.randint(800, 3000), owner=user.name))
		print(f"Created {DEMO_COMPANY}: {len(items)} items, {len(customers)} customers, {bills} paid bills.")
		print("Sign in with demo@example.com / Demo@1234")


def cmd_expire_trials(_args):
	from .services.accounts import expire_trials

	with session_scope() as db:
		print(f"Expired {expire_trials(db)} trial(s).")


def main():
	parser = argparse.ArgumentParser(prog="python -m app.cli")
	sub = parser.add_subparsers(dest="command", required=True)
	sub.add_parser("init-db")
	admin = sub.add_parser("create-admin")
	admin.add_argument("email")
	admin.add_argument("password")
	admin.add_argument("--name", default="Administrator")
	store = sub.add_parser("create-store")
	store.add_argument("store")
	store.add_argument("email")
	store.add_argument("password")
	store.add_argument("--name", default="Store Manager")
	demo = sub.add_parser("seed-demo")
	demo.add_argument("--days", type=int, default=60)
	sub.add_parser("expire-trials")
	args = parser.parse_args()

	init_db()
	handlers = {
		"init-db": lambda _a: print("Database ready."), "create-admin": cmd_create_admin,
		"create-store": cmd_create_store, "seed-demo": cmd_seed_demo, "expire-trials": cmd_expire_trials,
	}
	handlers[args.command](args)


if __name__ == "__main__":
	main()
