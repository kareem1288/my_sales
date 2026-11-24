"""Passwords, sessions, request context, permissions and rate limiting."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from .models import Company, RateLimit, RoleProfile, User, UserCompany
from .utils import AppError, AuthenticationError, PermissionError_, now

MANAGER_ROLE = "Store Manager"

# ---------------------------------------------------------------- passwords

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password):
	salt = secrets.token_bytes(16)
	digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
	return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
	try:
		scheme, salt_hex, digest_hex = (stored or "").split("$")
	except ValueError:
		return False
	if scheme != "scrypt":
		return False
	digest = hashlib.scrypt((password or "").encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
	return hmac.compare_digest(digest.hex(), digest_hex)


_DUMMY_HASH = None


def authenticate(db, email, password):
	"""Return the enabled user for these credentials or raise a generic error."""
	global _DUMMY_HASH
	email = (email or "").strip().lower()
	user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
	if not user or not user.password_hash:
		# Spend comparable time so response timing does not reveal which emails exist.
		_DUMMY_HASH = _DUMMY_HASH or hash_password("not-a-real-password")
		verify_password(password, _DUMMY_HASH)
		raise AuthenticationError("Invalid login credentials")
	if not verify_password(password, user.password_hash):
		raise AuthenticationError("Invalid login credentials")
	if not user.enabled:
		raise AuthenticationError("User disabled or missing")
	return user


# ------------------------------------------------------------------ context


@dataclass
class Ctx:
	db: object
	request: object
	user: User | None = None
	companies: list = field(default_factory=list)

	@property
	def user_name(self):
		return self.user.name if self.user else "Guest"

	@property
	def is_guest(self):
		return self.user is None

	@property
	def roles(self):
		if not self.user:
			return set()
		if self.user.is_system_admin:
			return {"System Manager", MANAGER_ROLE, "Cashier", "Accounts"}
		profiles = [p.strip() for p in (self.user.role_profiles or "").split(",") if p.strip()]
		roles = set()
		if profiles:
			for profile in self.db.execute(select(RoleProfile).where(RoleProfile.name.in_(profiles))).scalars():
				roles.update(r.strip() for r in profile.roles.split(",") if r.strip())
		return roles or {"Cashier"}

	def require_login(self):
		if not self.user:
			raise AuthenticationError("Please sign in to continue.")

	def require_manager(self):
		self.require_login()
		if MANAGER_ROLE not in self.roles:
			raise PermissionError_("Only store managers can change this record.")

	@property
	def default_company(self):
		if not self.companies:
			return None
		if self.user and self.user.default_company in self.companies:
			return self.user.default_company
		return self.companies[0]

	def check_company(self, company):
		if company not in self.companies:
			raise AppError("Select a valid store.")
		return company


def load_user_companies(db, user):
	if not user:
		return []
	if user.is_system_admin:
		return list(db.execute(select(Company.name).order_by(Company.name)).scalars())
	return list(db.execute(
		select(UserCompany.company).where(UserCompany.user == user.name).order_by(UserCompany.company)
	).scalars())


def user_from_session(db, session):
	email = session.get("user")
	if not email:
		return None
	user = db.execute(select(User).where(User.name == email)).scalar_one_or_none()
	if not user or not user.enabled or user.session_version != session.get("v"):
		session.clear()
		return None
	return user


def login(request, user):
	request.session.clear()
	request.session.update({"user": user.name, "v": user.session_version, "csrf": secrets.token_urlsafe(24)})


def logout(request):
	request.session.clear()


def csrf_token(request):
	token = request.session.get("csrf")
	if not token:
		token = secrets.token_urlsafe(24)
		request.session["csrf"] = token
	return token


# ------------------------------------------------------------- rate limiting


def rate_limit(key, limit, seconds):
	"""Fixed-window limiter stored in the database (works across serverless instances).

	Uses its own transaction so attempts are counted even when the request itself
	fails and rolls back (e.g. a wrong password).
	"""
	from .db import session_scope

	current = now()
	with session_scope() as db:
		row = db.get(RateLimit, key)
		if not row or row.window_start + timedelta(seconds=seconds) <= current:
			if row:
				row.window_start, row.count = current, 1
			else:
				db.add(RateLimit(key=key, window_start=current, count=1))
			return
		if row.count >= limit:
			raise AppError("You have made too many requests. Please try again later.", status_code=429)
		row.count += 1
