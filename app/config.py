"""Runtime configuration, read from environment variables.

Everything that used to live in Frappe "Single" doctypes (UPI Configuration,
MSA Settings, Brevo OTP Settings, Email Account) is configured here instead so
secrets never sit in the database and the app runs on serverless hosts.
"""

import os
import secrets
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _bool(name, default=False):
	value = os.environ.get(name)
	if value is None:
		return default
	return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
	def __init__(self):
		# Configuration problems are collected instead of raised so a misconfigured
		# deployment shows a readable setup page rather than crashing on import.
		self.problems = []
		production = bool(os.environ.get("VERCEL")) or _bool("PRODUCTION")
		self.database_url = _normalize_db_url(os.environ.get("DATABASE_URL") or f"sqlite:///{BASE_DIR.parent / 'my_sales.db'}")
		if production and self.database_url.startswith("sqlite"):
			# Serverless filesystems are temporary: every bill would be lost.
			self.problems.append(
				"DATABASE_URL is not set. Add a hosted Postgres connection string (for example from Neon)."
			)
			self.database_url = "sqlite://"  # placeholder; never used while problems exist
		secret = os.environ.get("SECRET_KEY")
		if not secret:
			if production:
				self.problems.append("SECRET_KEY is not set. Add a long random string.")
			secret = "dev-insecure-" + secrets.token_hex(8)
		self.secret_key = secret
		self.secure_cookies = _bool("SECURE_COOKIES", bool(os.environ.get("VERCEL")))
		self.site_url = (os.environ.get("SITE_URL") or "").rstrip("/")
		self.timezone = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")
		self.cron_secret = os.environ.get("CRON_SECRET", "")

		# Trial
		self.trial_days = int(os.environ.get("TRIAL_DAYS", "20"))
		self.trial_contact = os.environ.get("TRIAL_CONTACT", "9014040855")

		# Outgoing email (OTP and password reset)
		self.smtp_host = os.environ.get("SMTP_HOST", "")
		self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
		self.smtp_user = os.environ.get("SMTP_USER", "")
		self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
		self.smtp_from = os.environ.get("SMTP_FROM", self.smtp_user)
		self.smtp_tls = _bool("SMTP_TLS", True)

		# Brevo SMS OTP
		self.brevo_api_key = os.environ.get("BREVO_API_KEY", "")
		self.brevo_sms_sender = os.environ.get("BREVO_SMS_SENDER", "")

		# UPI
		self.upi_enabled = _bool("UPI_ENABLED", True)
		self.upi_mode = os.environ.get("UPI_MODE", "Testing")
		self.upi_id = os.environ.get("UPI_ID", "")
		self.upi_payment_api_url = os.environ.get("UPI_PAYMENT_API_URL", "")
		self.upi_api_key = os.environ.get("UPI_API_KEY", "")
		self.upi_api_secret = os.environ.get("UPI_API_SECRET", "")
		self.upi_redirect_to = os.environ.get("UPI_REDIRECT_TO", "")

		# MSA assistant (local Ollama)
		self.msa_enabled = _bool("MSA_ENABLED", False)
		self.ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
		self.ollama_model = os.environ.get("OLLAMA_MODEL", "")

	@property
	def email_configured(self):
		return bool(self.smtp_host and self.smtp_from)

	@property
	def brevo_configured(self):
		return bool(self.brevo_api_key and self.brevo_sms_sender)


def _normalize_db_url(url):
	# Hosted Postgres providers hand out postgres:// URLs; SQLAlchemy needs a driver name.
	if url.startswith("postgres://"):
		url = "postgresql://" + url[len("postgres://"):]
	if url.startswith("postgresql://"):
		url = "postgresql+psycopg://" + url[len("postgresql://"):]
	return url


@lru_cache
def get_settings():
	return Settings()
