"""Signup, trials, password/OTP login and password reset."""

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import select, update

from ..auth import authenticate, hash_password, login, rate_limit
from ..config import get_settings
from ..models import Company, File, OtpChallenge, PasswordResetToken, SignupDetails, User
from ..registry import whitelist
from ..setup import LANGUAGES, create_company, create_user, ensure_global_data
from ..utils import AppError, AuthenticationError, add_days, next_name, now, parse_json, today
from .notify import send_email, send_otp_sms

OTP_EXPIRY_SECONDS = 300
MAX_OTP_ATTEMPTS = 5
ALLOWED_BUSINESS_TYPES = {
	"Retail Store", "Grocery Store", "Supermarket", "Pharmacy", "Fashion & Apparel",
	"Electronics", "Restaurant", "Wholesale", "Other",
}
ALLOWED_COUNTRIES = {"India", "United States", "United Kingdom", "United Arab Emirates"}
ALLOWED_CURRENCIES = {"INR", "USD", "GBP", "AED"}
ALLOWED_FINANCIAL_YEAR_STARTS = {"1 April", "1 January", "1 July", "1 October"}
ALLOWED_TAX_PREFERENCES = {"GST", "No Tax", "VAT", "Other"}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _client_ip(ctx):
	forwarded = ctx.request.headers.get("x-forwarded-for", "")
	return forwarded.split(",")[0].strip() or (ctx.request.client.host if ctx.request.client else "unknown")


# -------------------------------------------------------------------- trial


def expiry_message():
	return (f"Your {get_settings().trial_days}-day free trial has ended. "
		f"Please call or message {get_settings().trial_contact} to continue using My Sales.")


def validate_trial(db, user):
	"""Block a login when the user's trial has expired (and mark it expired)."""
	row = db.execute(select(SignupDetails).where(SignupDetails.user == user.name)).scalar_one_or_none()
	if not row:
		return
	if row.status == "Trial Expired" or (row.trial_end_date and row.trial_end_date < today()):
		row.status = "Trial Expired"
		user.enabled = False
		db.commit()
		raise AuthenticationError(expiry_message())


def expire_trials(db):
	"""Disable users whose trial has passed (daily cron)."""
	rows = db.execute(select(SignupDetails).where(
		SignupDetails.status == "Active Trial", SignupDetails.trial_end_date < today()
	)).scalars().all()
	for row in rows:
		if row.user:
			db.execute(update(User).where(User.name == row.user).values(enabled=False))
		row.status = "Trial Expired"
	return len(rows)


@whitelist("store_management.signup.get_trial_login_status", allow_guest=True)
def get_trial_login_status(ctx, email=None):
	row = ctx.db.execute(select(SignupDetails).where(
		SignupDetails.email_address == (email or "").strip().lower()
	)).scalar_one_or_none()
	expired = bool(row and row.status == "Trial Expired")
	return {"expired": expired, "contact": get_settings().trial_contact if expired else None}


# ------------------------------------------------------------------- signup


@whitelist("store_management.signup.create_signup_details", allow_guest=True, methods=("POST",))
def create_signup_details(ctx, data=None):
	rate_limit(f"signup:{_client_ip(ctx)}", 5, 3600)
	db = ctx.db
	data = parse_json(data, {}) or {}
	if not isinstance(data, dict):
		raise AppError("Invalid signup details.")
	get = lambda key: str(data.get(key) or "").strip()  # noqa: E731

	email = get("email_address").lower()
	phone = re.sub(r"[\s()-]", "", get("phone_number"))
	if get("country") == "India" and re.fullmatch(r"[6-9]\d{9}", phone):
		phone = "+91" + phone
	if not EMAIL_RE.fullmatch(email):
		raise AppError("Enter a valid email address.")
	if not re.fullmatch(r"\+?\d{7,15}", phone):
		raise AppError("Enter a valid phone number.")
	if db.execute(select(SignupDetails.id).where(SignupDetails.email_address == email)).first():
		raise AppError("A signup request already exists for this email address.")
	if db.execute(select(User.id).where(User.email == email)).first():
		raise AppError("A user account already exists for this email address.")
	store_name = get("store_name")
	if db.execute(select(Company.id).where(Company.name == store_name)).first():
		raise AppError("A company already exists with this store name.")
	password = str(data.get("password") or "")
	if len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"[0-9\W]", password):
		raise AppError("Password must contain at least 8 characters, one uppercase letter, and one number or special character.")
	if any(not get(f) for f in ("full_name", "store_name", "store_address", "city", "pin_code", "state")):
		raise AppError("Complete all required account and store details.")
	if get("business_type") not in ALLOWED_BUSINESS_TYPES or get("country") not in ALLOWED_COUNTRIES:
		raise AppError("Select valid business and country details.")
	if get("currency") not in ALLOWED_CURRENCIES:
		raise AppError("Select a valid currency.")
	if get("financial_year_start") not in ALLOWED_FINANCIAL_YEAR_STARTS:
		raise AppError("Select a valid financial year start.")
	if get("tax_preference") not in ALLOWED_TAX_PREFERENCES:
		raise AppError("Select a valid tax preference.")
	language = get("language") or "en"
	if language not in LANGUAGES:
		raise AppError("Select a valid application language.")

	ensure_global_data(db)
	company = create_company(
		db, store_name, default_currency=get("currency"), country=get("country"), language=language,
		address=", ".join(filter(None, [get("store_address"), get("city"), get("state"), get("pin_code")])),
		phone=phone,
	)
	first, _, last = get("full_name").partition(" ")
	user = create_user(
		db, email, first, password_hash=hash_password(password), company=company.name, last_name=last or None,
		mobile_no=phone, language=language, role_profiles="Store Manager",
	)

	trial_start = today()
	trial_end = add_days(trial_start, get_settings().trial_days)
	doc = SignupDetails(
		name=next_name(db, "SIGNUP"), full_name=get("full_name"), email_address=email, phone_number=phone,
		store_name=store_name, business_type=get("business_type"), store_address=get("store_address"),
		city=get("city"), pin_code=get("pin_code"), state=get("state"), country=get("country"), language=language,
		currency=get("currency"), financial_year_start=get("financial_year_start"),
		tax_preference=get("tax_preference"), company=company.name, user=user.name,
		trial_start_date=trial_start, trial_end_date=trial_end, status="Active Trial",
	)
	db.add(doc)

	logo = data.get("store_logo")
	if logo and isinstance(logo, str) and logo.startswith("data:image/"):
		header, _, encoded = logo.partition(",")
		extension = header.split("/")[1].split(";")[0].replace("jpeg", "jpg")
		if extension not in {"png", "jpg", "webp"}:
			raise AppError("Upload a PNG, JPG, or WEBP logo.")
		try:
			content = base64.b64decode(encoded, validate=True)
		except ValueError:
			raise AppError("The store logo could not be read.")
		if len(content) > 2 * 1024 * 1024:
			raise AppError("Store logo must be smaller than 2 MB.")
		file = File(
			file_key=secrets.token_hex(16), file_name=f"store-logo.{extension}",
			content_type=f"image/{'jpeg' if extension == 'jpg' else extension}", content=content,
			company=company.name, owner=user.name,
		)
		db.add(file)
		doc.store_logo = f"/files/{file.file_key}/{file.file_name}"
	db.flush()
	return {
		"name": doc.name, "email": email, "store_name": store_name, "country": doc.country,
		"language": language, "trial_end_date": str(trial_end), "provisioned": True,
	}


# -------------------------------------------------------------------- login


def _finish_login(ctx, user):
	validate_trial(ctx.db, user)
	login(ctx.request, user)
	return {"status": "Logged In", "redirect_to": "/pos", "full_name": user.full_name}


@whitelist("store_management.otp_auth.password_login", "login", allow_guest=True, methods=("POST",))
def password_login(ctx, email=None, password=None, usr=None, pwd=None):
	email = (email or usr or "").strip().lower()
	rate_limit(f"login:{email}", 10, 300)
	user = authenticate(ctx.db, email, password or pwd)
	return _finish_login(ctx, user)


def _mask_email(email):
	name, domain = email.split("@", 1)
	return f"{name[:2]}{'*' * max(2, len(name) - 2)}@{domain}"


def _mask_mobile(number):
	digits = "".join(filter(str.isdigit, number or ""))
	return f"******{digits[-4:]}" if len(digits) >= 4 else "registered mobile"


def _challenge(ctx, challenge_id):
	row = ctx.db.get(OtpChallenge, str(challenge_id or ""))
	if not row or row.expires_at < now():
		raise AuthenticationError("This login verification has expired. Please sign in again.")
	return row


@whitelist("store_management.otp_auth.begin_otp_login", allow_guest=True, methods=("POST",))
def begin_otp_login(ctx, email=None, password=None):
	email = (email or "").strip().lower()
	rate_limit(f"login:{email}", 10, 300)
	user = authenticate(ctx.db, email, password)
	validate_trial(ctx.db, user)
	challenge_id = secrets.token_urlsafe(24)
	# Credentials were validated above and are never stored in the challenge.
	ctx.db.add(OtpChallenge(id=challenge_id, user=user.name, expires_at=now() + timedelta(seconds=OTP_EXPIRY_SECONDS)))
	settings = get_settings()
	return {
		"challenge_id": challenge_id,
		"email_available": bool(user.email) and settings.email_configured,
		"mobile_available": bool(user.mobile_no) and settings.brevo_configured,
		"masked_email": _mask_email(user.email) if user.email else None,
		"masked_mobile": _mask_mobile(user.mobile_no) if user.mobile_no else None,
	}


def _otp_hash(challenge_id, otp):
	return hashlib.sha256(f"{challenge_id}:{otp}".encode()).hexdigest()


@whitelist("store_management.otp_auth.send_login_otp", allow_guest=True, methods=("POST",))
def send_login_otp(ctx, challenge_id=None, delivery=None):
	rate_limit(f"otp-send:{challenge_id}", 5, 300)
	challenge = _challenge(ctx, challenge_id)
	user = ctx.db.execute(select(User).where(User.name == challenge.user)).scalar_one()
	delivery = (delivery or "").lower()
	otp = f"{secrets.randbelow(1_000_000):06d}"
	if delivery == "email" and user.email:
		send_email(
			user.email, "My Sales login verification code",
			f"{otp} is your My Sales login verification code. It expires in 5 minutes. Do not share it with anyone.",
		)
		masked = _mask_email(user.email)
	elif delivery == "mobile" and user.mobile_no:
		send_otp_sms(user.mobile_no, otp)
		masked = _mask_mobile(user.mobile_no)
	else:
		raise AppError("The selected OTP delivery method is unavailable for this user.")
	challenge.otp_hash = _otp_hash(challenge.id, otp)
	challenge.attempts = 0
	challenge.delivery = delivery
	challenge.expires_at = now() + timedelta(seconds=OTP_EXPIRY_SECONDS)
	return {"delivery": delivery, "masked_destination": masked, "expires_in": OTP_EXPIRY_SECONDS}


@whitelist("store_management.otp_auth.verify_login_otp", allow_guest=True, methods=("POST",))
def verify_login_otp(ctx, challenge_id=None, otp=None):
	rate_limit(f"otp-verify:{challenge_id}", 10, 300)
	db = ctx.db
	challenge = _challenge(ctx, challenge_id)
	if not challenge.otp_hash:
		raise AuthenticationError("Choose where to receive your OTP first.")
	if challenge.attempts >= MAX_OTP_ATTEMPTS:
		db.delete(challenge)
		db.commit()
		raise AuthenticationError("Too many incorrect attempts. Please sign in again.")
	if not hmac.compare_digest(challenge.otp_hash, _otp_hash(challenge.id, str(otp or "").strip())):
		challenge.attempts += 1
		db.commit()
		raise AuthenticationError("Incorrect verification code.")
	user = db.execute(select(User).where(User.name == challenge.user)).scalar_one()
	db.delete(challenge)
	if not user.enabled:
		raise AuthenticationError("User disabled or missing")
	return _finish_login(ctx, user)


# ----------------------------------------------------------- password reset


@whitelist("store_management.api.forgot_password_helper", allow_guest=True, methods=("POST",))
def forgot_password_helper(ctx, email=None):
	email = (email or "").strip().lower()
	rate_limit(f"reset:{_client_ip(ctx)}", 5, 900)
	if not get_settings().email_configured:
		return {"status": "error", "message": "No outgoing email account configured. Please contact administrator."}
	generic = {"status": "success", "message": "If an account exists for this email, a password reset link has been sent."}
	user = ctx.db.execute(select(User).where(User.email == email, User.enabled.is_(True))).scalar_one_or_none()
	if not user:
		return generic
	token = secrets.token_urlsafe(32)
	ctx.db.add(PasswordResetToken(
		token_hash=hashlib.sha256(token.encode()).hexdigest(), user=user.name, expires_at=now() + timedelta(hours=1),
	))
	base = get_settings().site_url or str(ctx.request.base_url).rstrip("/")
	send_email(
		user.email, "Password Reset Request",
		f"Hello {user.first_name},\n\nUse this link to set a new My Sales password. It expires in one hour.\n\n"
		f"{base}/store-login/reset-password?token={token}\n\nIf you did not request this, you can ignore this email.",
	)
	return generic


@whitelist("store_management.api.reset_password", allow_guest=True, methods=("POST",))
def reset_password(ctx, token=None, new_password=None):
	from .masters import _check_password_strength

	row = ctx.db.get(PasswordResetToken, hashlib.sha256(str(token or "").encode()).hexdigest())
	if not row or row.used or row.expires_at < now():
		raise AppError("This password reset link is invalid or has expired.")
	_check_password_strength(new_password or "")
	user = ctx.db.execute(select(User).where(User.name == row.user)).scalar_one()
	user.password_hash = hash_password(new_password)
	user.session_version += 1
	row.used = True
	return {"status": "success", "message": "Your password has been updated. Please sign in."}
