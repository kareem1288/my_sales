"""Database tables.

These replace the ERPNext DocTypes the original app depended on (Company, Item,
Customer, Sales Invoice, Payment Entry, ...) plus the app's own DocTypes
(Sign Up Details, Custom Reports). Every store-owned row carries a `company`
column so each signed-up store only ever sees its own data.
"""

from datetime import date, datetime

from sqlalchemy import (
	Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .utils import now


def _now():
	return now()


class Timestamped:
	creation: Mapped[datetime] = mapped_column(DateTime, default=_now)
	modified: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Company(Timestamped, Base):
	__tablename__ = "company"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(140), unique=True)
	company_name: Mapped[str] = mapped_column(String(140))
	abbr: Mapped[str] = mapped_column(String(10), unique=True)
	default_currency: Mapped[str] = mapped_column(String(10), default="INR")
	country: Mapped[str] = mapped_column(String(80), default="India")
	custom_application_language: Mapped[str] = mapped_column(String(10), default="en")
	address: Mapped[str | None] = mapped_column(Text)
	tax_id: Mapped[str | None] = mapped_column(String(40))
	phone: Mapped[str | None] = mapped_column(String(20))


class User(Timestamped, Base):
	__tablename__ = "user"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(140), unique=True)  # email, like Frappe
	email: Mapped[str] = mapped_column(String(140), unique=True)
	first_name: Mapped[str] = mapped_column(String(140))
	last_name: Mapped[str | None] = mapped_column(String(140))
	mobile_no: Mapped[str | None] = mapped_column(String(20))
	password_hash: Mapped[str | None] = mapped_column(String(255))
	enabled: Mapped[bool] = mapped_column(Boolean, default=True)
	language: Mapped[str] = mapped_column(String(10), default="en")
	role_profiles: Mapped[str] = mapped_column(String(500), default="")
	is_system_admin: Mapped[bool] = mapped_column(Boolean, default=False)
	default_company: Mapped[str | None] = mapped_column(String(140))
	session_version: Mapped[int] = mapped_column(Integer, default=1)

	@property
	def full_name(self):
		return " ".join(part for part in [self.first_name, self.last_name] if part)


class UserCompany(Base):
	__tablename__ = "user_company"
	user: Mapped[str] = mapped_column(String(140), primary_key=True)
	company: Mapped[str] = mapped_column(String(140), primary_key=True)


class RoleProfile(Base):
	__tablename__ = "role_profile"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(140), unique=True)
	role_profile: Mapped[str] = mapped_column(String(140))
	roles: Mapped[str] = mapped_column(String(500), default="")


class UOM(Base):
	__tablename__ = "uom"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(40), unique=True)
	uom_name: Mapped[str] = mapped_column(String(40))


class CompanyRecord(Timestamped):
	id: Mapped[int] = mapped_column(primary_key=True)
	company: Mapped[str] = mapped_column(String(140), index=True)
	name: Mapped[str] = mapped_column(String(140))


class ItemGroup(CompanyRecord, Base):
	__tablename__ = "item_group"
	__table_args__ = (UniqueConstraint("company", "name"),)
	item_group_name: Mapped[str] = mapped_column(String(140))
	parent_item_group: Mapped[str | None] = mapped_column(String(140))
	is_group: Mapped[bool] = mapped_column(Boolean, default=False)
	image: Mapped[str | None] = mapped_column(String(255))


class Item(CompanyRecord, Base):
	__tablename__ = "item"
	__table_args__ = (UniqueConstraint("company", "name"),)
	item_code: Mapped[str] = mapped_column(String(140))
	item_name: Mapped[str] = mapped_column(String(140))
	item_group: Mapped[str] = mapped_column(String(140))
	stock_uom: Mapped[str] = mapped_column(String(40), default="Nos")
	is_stock_item: Mapped[bool] = mapped_column(Boolean, default=True)
	is_sales_item: Mapped[bool] = mapped_column(Boolean, default=True)
	opening_stock: Mapped[float] = mapped_column(Float, default=0)
	stock_qty: Mapped[float] = mapped_column(Float, default=0)
	standard_rate: Mapped[float] = mapped_column(Float, default=0)
	image: Mapped[str | None] = mapped_column(String(255))
	barcode: Mapped[str | None] = mapped_column(String(80), index=True)
	description: Mapped[str | None] = mapped_column(Text)
	disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class CustomerGroup(CompanyRecord, Base):
	__tablename__ = "customer_group"
	__table_args__ = (UniqueConstraint("company", "name"),)
	customer_group_name: Mapped[str] = mapped_column(String(140))
	parent_customer_group: Mapped[str | None] = mapped_column(String(140))
	is_group: Mapped[bool] = mapped_column(Boolean, default=False)


class Customer(CompanyRecord, Base):
	__tablename__ = "customer"
	__table_args__ = (UniqueConstraint("company", "name"),)
	customer_name: Mapped[str] = mapped_column(String(140))
	customer_type: Mapped[str] = mapped_column(String(40), default="Individual")
	customer_group: Mapped[str | None] = mapped_column(String(140))
	territory: Mapped[str | None] = mapped_column(String(140))
	tax_id: Mapped[str | None] = mapped_column(String(40))
	mobile_no: Mapped[str | None] = mapped_column(String(20), index=True)
	disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class ItemTaxTemplate(CompanyRecord, Base):
	__tablename__ = "item_tax_template"
	__table_args__ = (UniqueConstraint("company", "name"),)
	title: Mapped[str] = mapped_column(String(140))
	disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class SalesInvoice(Timestamped, Base):
	__tablename__ = "sales_invoice"
	__table_args__ = (Index("ix_sinv_company_date", "company", "posting_date"),)
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(40), unique=True)
	company: Mapped[str] = mapped_column(String(140))
	customer: Mapped[str] = mapped_column(String(140))
	customer_name: Mapped[str] = mapped_column(String(140))
	posting_date: Mapped[date] = mapped_column(Date)
	posting_time: Mapped[str] = mapped_column(String(8))
	total_qty: Mapped[float] = mapped_column(Float, default=0)
	net_total: Mapped[float] = mapped_column(Float, default=0)
	discount_amount: Mapped[float] = mapped_column(Float, default=0)
	grand_total: Mapped[float] = mapped_column(Float, default=0)
	rounding_adjustment: Mapped[float] = mapped_column(Float, default=0)
	rounded_total: Mapped[float] = mapped_column(Float, default=0)
	paid_amount: Mapped[float] = mapped_column(Float, default=0)
	outstanding_amount: Mapped[float] = mapped_column(Float, default=0)
	docstatus: Mapped[int] = mapped_column(Integer, default=1)  # 0 draft, 1 submitted, 2 cancelled
	remarks: Mapped[str | None] = mapped_column(Text)
	contact_mobile: Mapped[str | None] = mapped_column(String(20))
	owner: Mapped[str | None] = mapped_column(String(140))
	items: Mapped[list["SalesInvoiceItem"]] = relationship(
		back_populates="invoice", cascade="all, delete-orphan", order_by="SalesInvoiceItem.idx"
	)


class SalesInvoiceItem(Base):
	__tablename__ = "sales_invoice_item"
	id: Mapped[int] = mapped_column(primary_key=True)
	invoice_id: Mapped[int] = mapped_column(ForeignKey("sales_invoice.id", ondelete="CASCADE"), index=True)
	idx: Mapped[int] = mapped_column(Integer, default=1)
	item_code: Mapped[str] = mapped_column(String(140))
	item_name: Mapped[str] = mapped_column(String(140))
	item_group: Mapped[str | None] = mapped_column(String(140))
	qty: Mapped[float] = mapped_column(Float)
	rate: Mapped[float] = mapped_column(Float)
	amount: Mapped[float] = mapped_column(Float)
	invoice: Mapped[SalesInvoice] = relationship(back_populates="items")


class Payment(Timestamped, Base):
	__tablename__ = "payment"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(40), unique=True)
	company: Mapped[str] = mapped_column(String(140), index=True)
	invoice: Mapped[str] = mapped_column(String(40), index=True)
	customer: Mapped[str] = mapped_column(String(140))
	posting_date: Mapped[date] = mapped_column(Date)
	mode_of_payment: Mapped[str] = mapped_column(String(40))
	amount: Mapped[float] = mapped_column(Float)
	reference_no: Mapped[str | None] = mapped_column(String(140))


class Expense(Timestamped, Base):
	"""Purchases/expenses; replaces Purchase Invoice totals on the dashboard and P&L."""

	__tablename__ = "expense"
	__table_args__ = (UniqueConstraint("company", "name"),)
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(40))
	company: Mapped[str] = mapped_column(String(140), index=True)
	posting_date: Mapped[date] = mapped_column(Date)
	description: Mapped[str] = mapped_column(String(255))
	amount: Mapped[float] = mapped_column(Float)
	owner: Mapped[str | None] = mapped_column(String(140))


class CustomReport(Base):
	__tablename__ = "custom_report"
	__table_args__ = (UniqueConstraint("company", "report_name"),)
	id: Mapped[int] = mapped_column(primary_key=True)
	company: Mapped[str] = mapped_column(String(140), index=True)
	report_name: Mapped[str] = mapped_column(String(140))
	route_key: Mapped[str] = mapped_column(String(140))
	is_visible: Mapped[bool] = mapped_column(Boolean, default=True)
	is_default: Mapped[bool] = mapped_column(Boolean, default=False)
	display_order: Mapped[int] = mapped_column(Integer, default=100)
	accent_color: Mapped[str] = mapped_column(String(20), default="#168650")
	report_description: Mapped[str | None] = mapped_column(Text)


class SignupDetails(Timestamped, Base):
	__tablename__ = "signup_details"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(40), unique=True)
	full_name: Mapped[str] = mapped_column(String(140))
	email_address: Mapped[str] = mapped_column(String(140), unique=True)
	phone_number: Mapped[str] = mapped_column(String(20))
	store_name: Mapped[str] = mapped_column(String(140))
	business_type: Mapped[str] = mapped_column(String(40))
	store_address: Mapped[str] = mapped_column(Text)
	city: Mapped[str] = mapped_column(String(80))
	pin_code: Mapped[str] = mapped_column(String(20))
	state: Mapped[str] = mapped_column(String(80))
	country: Mapped[str] = mapped_column(String(80))
	language: Mapped[str] = mapped_column(String(10))
	currency: Mapped[str] = mapped_column(String(10))
	financial_year_start: Mapped[str] = mapped_column(String(20))
	tax_preference: Mapped[str] = mapped_column(String(20))
	store_logo: Mapped[str | None] = mapped_column(String(255))
	company: Mapped[str | None] = mapped_column(String(140))
	user: Mapped[str | None] = mapped_column(String(140), index=True)
	trial_start_date: Mapped[date | None] = mapped_column(Date)
	trial_end_date: Mapped[date | None] = mapped_column(Date)
	status: Mapped[str] = mapped_column(String(40), default="Pending")


class File(Timestamped, Base):
	"""Uploaded files are stored in the database so the app works on read-only/serverless filesystems."""

	__tablename__ = "file"
	id: Mapped[int] = mapped_column(primary_key=True)
	file_key: Mapped[str] = mapped_column(String(64), unique=True)
	file_name: Mapped[str] = mapped_column(String(255))
	content_type: Mapped[str] = mapped_column(String(100))
	content: Mapped[bytes] = mapped_column(LargeBinary)
	company: Mapped[str | None] = mapped_column(String(140))
	owner: Mapped[str | None] = mapped_column(String(140))


class OtpChallenge(Base):
	__tablename__ = "otp_challenge"
	id: Mapped[str] = mapped_column(String(64), primary_key=True)
	user: Mapped[str] = mapped_column(String(140))
	otp_hash: Mapped[str | None] = mapped_column(String(64))
	attempts: Mapped[int] = mapped_column(Integer, default=0)
	delivery: Mapped[str | None] = mapped_column(String(10))
	expires_at: Mapped[datetime] = mapped_column(DateTime)


class PasswordResetToken(Base):
	__tablename__ = "password_reset_token"
	token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
	user: Mapped[str] = mapped_column(String(140))
	expires_at: Mapped[datetime] = mapped_column(DateTime)
	used: Mapped[bool] = mapped_column(Boolean, default=False)


class RateLimit(Base):
	__tablename__ = "rate_limit"
	key: Mapped[str] = mapped_column(String(255), primary_key=True)
	window_start: Mapped[datetime] = mapped_column(DateTime)
	count: Mapped[int] = mapped_column(Integer, default=0)


class Counter(Base):
	"""Naming series counters, e.g. SINV-2026 -> 42."""

	__tablename__ = "counter"
	key: Mapped[str] = mapped_column(String(80), primary_key=True)
	value: Mapped[int] = mapped_column(Integer, default=0)
