"""Small helpers replacing frappe.utils."""

import calendar
import json
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo


class AppError(Exception):
	"""A user-facing validation error (frappe.throw)."""

	status_code = 417
	exc_type = "ValidationError"

	def __init__(self, message, status_code=None):
		super().__init__(message)
		self.message = message
		if status_code:
			self.status_code = status_code


class PermissionError_(AppError):
	status_code = 403
	exc_type = "PermissionError"


class AuthenticationError(AppError):
	status_code = 401
	exc_type = "AuthenticationError"


class NotFoundError(AppError):
	status_code = 404
	exc_type = "DoesNotExistError"


def throw(message, exc=AppError):
	raise exc(message)


def _tz():
	from .config import get_settings

	return ZoneInfo(get_settings().timezone)


def now():
	"""Naive local datetime in the store's configured timezone."""
	return datetime.now(_tz()).replace(tzinfo=None, microsecond=0)


def today():
	return now().date()


def getdate(value=None):
	if value is None or value == "":
		return today()
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def add_days(value, days):
	return getdate(value) + timedelta(days=int(days))


def first_day(value):
	value = getdate(value)
	return value.replace(day=1)


def last_day(value):
	value = getdate(value)
	return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def flt(value, precision=None):
	try:
		number = float(value or 0)
	except (TypeError, ValueError):
		number = 0.0
	if precision is not None:
		number = money_round(number, precision)
	return number


def money_round(value, precision=2):
	quant = Decimal(1).scaleb(-precision)
	return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def parse_json(value, default=None):
	if value is None or value == "":
		return default
	if isinstance(value, (dict, list, int, float, bool)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return value


def cint(value):
	value = parse_json(value, 0)
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 1 if str(value).lower() in {"true", "yes", "on"} else 0


def fmt_money(value, precision=2, currency=None):
	value = flt(value, precision)
	negative = value < 0
	integer, _, fraction = f"{abs(value):.{precision}f}".partition(".")
	# Indian digit grouping: 12,34,567.00
	if len(integer) > 3:
		head, tail = integer[:-3], integer[-3:]
		groups = []
		while len(head) > 2:
			groups.insert(0, head[-2:])
			head = head[:-2]
		if head:
			groups.insert(0, head)
		integer = ",".join(groups + [tail])
	text = integer + (f".{fraction}" if precision else "")
	symbol = {"INR": "₹", "USD": "$", "GBP": "£", "AED": "AED "}.get(currency or "", "")
	return ("-" if negative else "") + symbol + text


def formatdate(value, fmt="dd-MM-yyyy"):
	value = getdate(value)
	return fmt.replace("dd", f"{value.day:02d}").replace("MM", f"{value.month:02d}").replace("yyyy", f"{value.year:04d}")


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
	"Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words_below_1000(number):
	words = []
	if number >= 100:
		words += [_ONES[number // 100], "Hundred"]
		number %= 100
	if number >= 20:
		words.append(_TENS[number // 10])
		number %= 10
	if number:
		words.append(_ONES[number])
	return words


def in_words(amount, currency="INR"):
	"""Indian-system amount in words, e.g. 'INR One Thousand Two Hundred only.'"""
	amount = money_round(amount, 2)
	rupees = int(amount)
	paise = int(round((amount - rupees) * 100))
	if rupees == 0 and paise == 0:
		return f"{currency} Zero only."
	words = []
	for divisor, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
		if rupees >= divisor:
			words += _words_below_1000(rupees // divisor) + [label]
			rupees %= divisor
	words += _words_below_1000(rupees)
	text = f"{currency} {' '.join(words)}".strip()
	if paise:
		text += f" and {' '.join(_words_below_1000(paise))} paisa"
	return text + " only."


def row_to_dict(row, exclude=("content", "password_hash", "session_version")):
	result = {}
	for column in row.__table__.columns:
		if column.key in exclude:
			continue
		value = getattr(row, column.key)
		if isinstance(value, (date, datetime)):
			value = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
		elif isinstance(value, bool):
			value = int(value)
		result[column.key] = value
	return result


def next_name(db, prefix, digits=5):
	"""Allocate the next number in a naming series (e.g. SINV-2026-00001)."""
	from sqlalchemy import select

	from .models import Counter

	counter = db.execute(select(Counter).where(Counter.key == prefix).with_for_update()).scalar_one_or_none()
	if not counter:
		counter = Counter(key=prefix, value=0)
		db.add(counter)
	counter.value += 1
	db.flush()
	return f"{prefix}-{counter.value:0{digits}d}"
