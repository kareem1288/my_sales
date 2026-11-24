"""MSA — My Sales Assistant: store report shortcuts plus optional local Ollama answers."""

import json
import re
from urllib.parse import urlparse

import requests
from sqlalchemy import func, select

from ..auth import rate_limit
from ..config import get_settings
from ..models import Item, Payment, SalesInvoice, SalesInvoiceItem
from ..registry import whitelist
from ..utils import AppError, add_days, flt, today

SHORTCUTS = {
	"show today's sales", "today's sales summary", "show today’s sales summary", "show today's sales summary",
	"today's sales", "show this week's sales", "this week's sales", "top 5 selling items", "top selling items",
	"which items are low in stock", "low stock items", "cash collected today",
}


@whitelist("store_management.assistant.ask", methods=("POST",))
def ask(ctx, question=None, history=None):
	rate_limit(f"msa:{ctx.user_name}", 30, 60)
	original = str(question or "").strip()
	if not original or len(original) > 4000:
		raise AppError("Enter a question of up to 4,000 characters.")
	normalized = re.sub(r"[?!.,]+$", "", original.lower()).strip()
	company = ctx.default_company
	if normalized not in SHORTCUTS or not company:
		return _ask_general(original, history)

	db = ctx.db
	end = today()
	start = add_days(end, -end.weekday()) if "week" in normalized else end
	if "stock" in normalized:
		rows = db.execute(
			select(Item.item_code, Item.item_name, Item.stock_qty).where(
				Item.company == company, Item.is_stock_item.is_(True), Item.disabled.is_(False), Item.stock_qty <= 5
			).order_by(Item.stock_qty).limit(10)
		).all()
		if not rows:
			return {"text": "No low-stock items found in your inventory."}
		return {"text": "Low-stock items (5 units or fewer):\n" + "\n".join(f"{code} · {name}: {flt(qty):g}" for code, name, qty in rows)}

	in_range = (SalesInvoice.company == company, SalesInvoice.docstatus == 1, SalesInvoice.posting_date.between(start, end))
	if "top" in normalized or "selling" in normalized:
		rows = db.execute(
			select(SalesInvoiceItem.item_name, func.sum(SalesInvoiceItem.qty).label("sold"))
			.join(SalesInvoice).where(*in_range).group_by(SalesInvoiceItem.item_name)
			.order_by(func.sum(SalesInvoiceItem.qty).desc()).limit(5)
		).all()
		if not rows:
			return {"text": "No sales found for this period."}
		return {"text": "Top selling items for this period:\n" + "\n".join(f"{name}: {flt(sold):g} sold" for name, sold in rows)}
	if "cash" in normalized:
		total = db.execute(select(func.sum(Payment.amount)).where(
			Payment.company == company, Payment.mode_of_payment == "Cash", Payment.posting_date == end
		)).scalar()
		return {"text": f"Cash collected today ({end}): ₹{flt(total):,.2f}"}

	invoices = db.execute(select(SalesInvoice).where(*in_range)).scalars().all()
	hourly = {}
	for inv in invoices:
		hour = str(inv.posting_time or "00").split(":")[0]
		hourly[hour] = hourly.get(hour, 0) + flt(inv.grand_total)
	return {
		"text": f"Sales summary: {start} – {end}",
		"trend": [{"label": f"{hour}:00", "value": round(value, 2)} for hour, value in sorted(hourly.items())],
		"metrics": {
			"Sales": f"₹{sum(flt(i.grand_total) for i in invoices):,.2f}", "Bills": len(invoices),
			"Items Sold": sum(flt(i.total_qty) for i in invoices), "Customers": len({i.customer for i in invoices}),
		},
	}


def _history(history):
	history = json.loads(history) if isinstance(history, str) and history else history
	if history is None or history == "":
		return []
	if not isinstance(history, list) or len(history) > 12:
		raise AppError("Conversation history must contain at most 12 messages.")
	clean = []
	for message in history:
		if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
			raise AppError("Invalid conversation message.")
		content = message.get("content")
		if not isinstance(content, str) or len(content) > 8000:
			raise AppError("Invalid conversation message length.")
		clean.append({"role": message["role"], "content": content})
	if sum(len(m["content"]) for m in clean) > 24000:
		raise AppError("Conversation history is too long. Start a new chat.")
	return clean


def _ask_general(question, history):
	settings = get_settings()
	if not settings.msa_enabled:
		return {"text": "Ask an administrator to enable general answers (MSA_ENABLED) and configure Ollama. No API key is required."}
	model = settings.ollama_model.strip()
	if not model:
		return {"text": "Set OLLAMA_MODEL to a downloaded Ollama model name. No API key is required."}
	if model.endswith("-cloud"):
		raise AppError("Choose a downloaded local model, not an Ollama cloud model.")
	base_url = settings.ollama_url.strip().rstrip("/")
	parsed = urlparse(base_url)
	if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query:
		raise AppError("Configure a valid OLLAMA_URL without credentials.")
	messages = [{"role": "system", "content": (
		"You are MSA, a helpful general-purpose assistant. Answer questions on any topic and match the user's language. "
		"You have no direct access to store records or live web search. Use verified store metrics in the conversation "
		"when provided, but never invent store data or pretend to search the internet or perform actions. "
		f"Be transparent about uncertainty for current facts. Keep answers clear. Today is {today()}."
	)}] + _history(history) + [{"role": "user", "content": question}]
	try:
		response = requests.post(
			base_url + "/api/chat", timeout=(10, 90), allow_redirects=False,
			json={"model": model, "messages": messages, "stream": False, "options": {"num_predict": 2000}},
		)
	except requests.RequestException:
		raise AppError("MSA cannot reach Ollama. Start the model server and check OLLAMA_URL.")
	if response.status_code == 404:
		raise AppError("The Ollama model was not found. Download it and check OLLAMA_MODEL.")
	if not response.ok or 300 <= response.status_code < 400:
		raise AppError("Ollama could not answer. Check the server and downloaded model configuration.")
	try:
		answer = response.json().get("message", {}).get("content", "").strip()
	except (ValueError, AttributeError, TypeError):
		raise AppError("Ollama returned an invalid response. Please try again.")
	if not answer:
		raise AppError("Ollama returned no answer. Please try again.")
	return {"text": answer, "sources": []}
