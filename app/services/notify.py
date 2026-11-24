"""Outgoing email (SMTP) and SMS (Brevo)."""

import logging
import smtplib
from email.message import EmailMessage

import requests

from ..config import get_settings
from ..utils import AppError

log = logging.getLogger(__name__)
BREVO_API_URL = "https://api.brevo.com/v3"


def send_email(recipient, subject, text):
	settings = get_settings()
	if not settings.email_configured:
		raise AppError("Outgoing email is not configured. Ask an administrator to set SMTP_HOST and SMTP_FROM.")
	message = EmailMessage()
	message["From"] = settings.smtp_from
	message["To"] = recipient
	message["Subject"] = subject
	message.set_content(text)
	try:
		if settings.smtp_port == 465:
			server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
		else:
			server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
			if settings.smtp_tls:
				server.starttls()
		with server:
			if settings.smtp_user:
				server.login(settings.smtp_user, settings.smtp_password)
			server.send_message(message)
	except (OSError, smtplib.SMTPException):
		log.exception("Email delivery failed")
		raise AppError("The email could not be sent. Please try again later.")


def send_otp_sms(recipient, otp):
	settings = get_settings()
	if not settings.brevo_configured:
		raise AppError("Mobile OTP is not configured. Set BREVO_API_KEY and BREVO_SMS_SENDER.")
	payload = {
		"sender": settings.brevo_sms_sender,
		"recipient": "".join(filter(str.isdigit, recipient or "")),
		"content": f"{otp} is your My Sales login code. It expires in 5 minutes. Do not share it.",
		"type": "transactional",
	}
	try:
		response = requests.post(
			f"{BREVO_API_URL}/transactionalSMS/send", json=payload, timeout=15,
			headers={"api-key": settings.brevo_api_key, "accept": "application/json", "content-type": "application/json"},
		)
	except requests.RequestException:
		log.exception("Brevo unreachable")
		raise AppError("Brevo could not be reached. Please try again.")
	if response.status_code not in {200, 201, 202, 204}:
		log.error("Brevo status %s: %s", response.status_code, response.text[:1000])
		try:
			detail = response.json().get("message")
		except (ValueError, AttributeError):
			detail = None
		raise AppError(detail or "Brevo rejected the OTP delivery request.")
