"""My Sales web application (FastAPI).

Routes mirror the original Frappe app so the existing frontend works unchanged:
  pages      /pos /masters /reports /assistant /more /store-login /store-signup
  API        /api/method/<dotted.method.name>   ->  {"message": ...}
  assets     /assets/store_management/...
"""

import csv
import json
import logging
import mimetypes
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import BASE_DIR, get_settings
from .db import SessionLocal, init_db, session_scope
from .models import Company, File, SignupDetails
from .registry import METHODS, call
from .setup import ensure_global_data
from .utils import AppError, fmt_money, formatdate, in_words

# Register whitelisted methods.
from .services import accounts, assistant, dashboard, masters, pos, reports  # noqa: F401,E402

log = logging.getLogger("my_sales")
settings = get_settings()

@asynccontextmanager
async def lifespan(_app):
	init_db()
	with session_scope() as db:
		ensure_global_data(db)
	yield


app = FastAPI(title="My Sales", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
	SessionMiddleware, secret_key=settings.secret_key, session_cookie="my_sales_sid",
	max_age=14 * 24 * 3600, same_site="lax", https_only=settings.secure_cookies,
)
app.mount("/assets/store_management", StaticFiles(directory=BASE_DIR / "static"), name="assets")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
BRAND_DIR = BASE_DIR / "brand"
PUBLIC_BRAND_ASSETS = {"my-sales-logo.svg", "my-sales-icon.svg", "my-sales-icon-192.png", "my-sales-icon-512.png"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_UPLOAD_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ANDROID_PACKAGE_ID = "cloud.frappe.m.mysales.twa"
ANDROID_UPLOAD_CERT_SHA256 = (
	"C7:F8:57:4A:B5:27:76:01:32:C2:00:1F:EC:F8:4D:70:FC:E4:81:E0:FF:5B:37:72:5F:AC:FF:E8:E1:6F:DD:F4"
)


# ------------------------------------------------------------------ helpers


def _translations(lang):
	path = BASE_DIR / "translations" / f"{lang}.csv"
	if lang == "en" or not path.exists():
		return {}
	with open(path, encoding="utf-8") as handle:
		return {row[0]: row[1] for row in csv.reader(handle) if len(row) >= 2}


def _safe_json(value):
	"""JSON for embedding inside a <script> tag."""
	return json.dumps(value, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _context(request, db):
	user = auth.user_from_session(db, request.session)
	return auth.Ctx(db=db, request=request, user=user, companies=auth.load_user_companies(db, user))


def _render(request, ctx, template, **context):
	lang = ctx.user.language if ctx.user else "en"
	boot = {
		"user": ctx.user_name, "full_name": ctx.user.full_name if ctx.user else "", "lang": lang,
		"csrf_token": auth.csrf_token(request), "company": ctx.default_company or "",
		"translations": _translations(lang),
	}
	context.update(
		request=request, frappe=SimpleNamespace(session=SimpleNamespace(user=ctx.user_name)),
		boot_json=_safe_json(boot), lang=lang,
	)
	response = templates.TemplateResponse(request, template, context)
	response.headers["Cache-Control"] = "no-store"
	return response


def _login_redirect(request):
	target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
	return RedirectResponse(f"/store-login?redirect-to={quote(target)}", status_code=302)


def _error_payload(message, exc_type="ValidationError"):
	return {
		"exc_type": exc_type, "exc": exc_type,
		"_server_messages": json.dumps([json.dumps({"message": message, "indicator": "red"})]),
	}


# -------------------------------------------------------------------- pages


def page(path, template, title, login_required=True, guest_only=False, **defaults):
	def view(request: Request):
		with SessionLocal() as db:
			ctx = _context(request, db)
			if login_required and ctx.is_guest:
				return _login_redirect(request)
			if guest_only and not ctx.is_guest:
				return RedirectResponse("/pos", status_code=302)
			extra = {key: value(ctx) if callable(value) else value for key, value in defaults.items()}
			response = _render(request, ctx, template, title=title, **extra)
			db.commit()
			return response

	app.add_api_route(path, view, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)


def _pos_context(ctx):
	try:
		data = pos.get_bootstrap(ctx)
	except AppError:
		log.exception("POS bootstrap failed")
		data = {"shop_name": "Retail POS", "company": None, "default_customer": "Walk-in Customer",
			"categories": [], "items": [], "summary": {"today_sales": 0, "today_bills": 0, "recent_bills": []}}
	return _safe_json(data)


page("/pos", "pages/pos.html", "My Sales", body_class="store-pos-page", insights_dashboard_url="",
	meta_description="My Sales mobile billing, master data, and reporting.", pos_bootstrap_json=_pos_context)
page("/masters", "pages/masters.html", "Masters · My Sales", body_class="store-masters-page")
page("/reports", "pages/reports.html", "Reports · My Sales", body_class="store-reports-page", insights_dashboards=[],
	configured_reports_json=lambda ctx: _safe_json(reports.configured_reports(ctx, visible_only=True)),
	default_company=lambda ctx: ctx.default_company or "")
page("/assistant", "pages/assistant.html", "MSA · My Sales Assistant")
page("/more", "pages/more.html", "More · My Sales", full_name=lambda ctx: ctx.user.full_name or ctx.user_name,
	user_email=lambda ctx: ctx.user.email, user_initial=lambda ctx: (ctx.user.full_name or ctx.user_name)[:1].upper())
page("/store-login", "pages/store-login.html", "Retail Billing Login", login_required=False, guest_only=True)
page("/store-signup", "pages/store-signup.html", "Create Your Store", login_required=False, guest_only=True)
page("/store-login/forgot-password", "pages/forgot-password.html", "Forgot Password - Retail Billing", login_required=False)
page("/store-login/reset-password", "pages/reset-password.html", "Set New Password - Retail Billing", login_required=False)


@app.get("/", include_in_schema=False)
@app.get("/login", include_in_schema=False)
@app.get("/app/login", include_in_schema=False)
def home(request: Request):
	return RedirectResponse("/pos" if request.session.get("user") else "/store-login", status_code=302)


@app.get("/desk", include_in_schema=False)
@app.get("/app", include_in_schema=False)
@app.get("/app/{rest:path}", include_in_schema=False)
def desk_redirect(request: Request, rest: str = ""):
	return RedirectResponse("/masters" if request.session.get("user") else "/store-login", status_code=302)


@app.get("/logout", include_in_schema=False)
@app.api_route("/api/method/logout", methods=["GET", "POST"], include_in_schema=False)
def logout(request: Request):
	auth.logout(request)
	return RedirectResponse("/store-login", status_code=302)


@app.get("/offline", include_in_schema=False)
def offline():
	return FileResponse(BASE_DIR / "templates" / "offline.html", media_type="text/html")


@app.get("/my-sales-sw.js", include_in_schema=False)
def service_worker():
	return FileResponse(
		BASE_DIR / "templates" / "my-sales-sw.js", media_type="text/javascript",
		headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
	)


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
def assetlinks():
	payload = [{
		"relation": ["delegate_permission/common.handle_all_urls"],
		"target": {"namespace": "android_app", "package_name": ANDROID_PACKAGE_ID,
			"sha256_cert_fingerprints": [ANDROID_UPLOAD_CERT_SHA256]},
	}]
	return JSONResponse(payload, headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"})


@app.get("/printview", include_in_schema=False)
def printview(request: Request, name: str = ""):
	with SessionLocal() as db:
		ctx = _context(request, db)
		if ctx.is_guest:
			return _login_redirect(request)
		try:
			invoice = pos.invoice_for_print(ctx, name)
		except AppError as error:
			raise HTTPException(status_code=error.status_code, detail=error.message)
		company = db.execute(select(Company).where(Company.name == invoice.company)).scalar_one()
		doc = SimpleNamespace(
			**{c.key: getattr(invoice, c.key) for c in invoice.__table__.columns},
			items=list(invoice.items), taxes=[], currency=company.default_currency, po_no=None,
			company_address_display=company.address, company_tax_id=company.tax_id,
			in_words=in_words(invoice.rounded_total, company.default_currency),
		)
		frappe_ns = SimpleNamespace(utils=SimpleNamespace(fmt_money=fmt_money, formatdate=formatdate))
		body = templates.get_template("receipt.html").render(doc=doc, frappe=frappe_ns)
		html = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
			f"<title>{invoice.name}</title><style>body{{margin:0;background:#fff}}@media print{{@page{{margin:4mm}}}}</style>"
			f"</head><body>{body}</body></html>")
		return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------- files & brand


@app.get("/api/method/store_management.api.get_brand_asset", include_in_schema=False)
def get_brand_asset(name: str = ""):
	if name not in PUBLIC_BRAND_ASSETS:
		raise HTTPException(status_code=404, detail="Brand asset not found.")
	return FileResponse(
		BRAND_DIR / name, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
		headers={"Cache-Control": "public, max-age=86400"},
	)


@app.post("/api/method/upload_file", include_in_schema=False)
async def upload_file(request: Request):
	with SessionLocal() as db:
		ctx = _context(request, db)
		if ctx.is_guest:
			return JSONResponse(_error_payload("Please sign in to upload files.", "AuthenticationError"), status_code=401)
		if not _csrf_ok(request):
			return JSONResponse(_error_payload("Invalid request. Refresh the page and try again.", "CSRFTokenError"), status_code=400)
		form = await request.form()
		upload = form.get("file")
		if upload is None or not hasattr(upload, "read"):
			return JSONResponse(_error_payload("Select a file to upload."), status_code=417)
		content = await upload.read(MAX_UPLOAD_BYTES + 1)
		if len(content) > MAX_UPLOAD_BYTES:
			return JSONResponse(_error_payload("Files must be smaller than 5 MB."), status_code=417)
		content_type = _sniff_image(content)
		if content_type not in ALLOWED_UPLOAD_TYPES:
			return JSONResponse(_error_payload("Upload a PNG, JPG, WEBP or GIF image."), status_code=417)
		extension = mimetypes.guess_extension(content_type) or ""
		file_name = "".join(c for c in Path(upload.filename or "image").stem if c.isalnum() or c in "-_")[:80] or "image"
		file_name += extension
		row = File(file_key=secrets.token_hex(16), file_name=file_name, content_type=content_type, content=content,
			company=ctx.default_company, owner=ctx.user_name)
		db.add(row)
		db.commit()
		url = f"/files/{row.file_key}/{quote(file_name)}"
		return {"message": {"name": row.file_key, "file_name": file_name, "file_url": url, "is_private": 1}}


def _sniff_image(content):
	if content.startswith(b"\x89PNG\r\n\x1a\n"):
		return "image/png"
	if content.startswith(b"\xff\xd8\xff"):
		return "image/jpeg"
	if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
		return "image/webp"
	if content[:6] in (b"GIF87a", b"GIF89a"):
		return "image/gif"
	return None


@app.get("/files/{file_key}/{file_name}", include_in_schema=False)
def get_file(request: Request, file_key: str, file_name: str):
	with SessionLocal() as db:
		ctx = _context(request, db)
		row = db.execute(select(File).where(File.file_key == file_key)).scalar_one_or_none()
		if ctx.is_guest or not row or (row.company not in ctx.companies and row.owner != ctx.user_name):
			raise HTTPException(status_code=404)
		return Response(row.content, media_type=row.content_type, headers={
			"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
			"Content-Disposition": f"inline; filename=\"{row.file_name}\"",
		})


# --------------------------------------------------------------- API methods


def _csrf_ok(request):
	expected = request.session.get("csrf")
	provided = request.headers.get("x-frappe-csrf-token", "")
	return bool(expected) and secrets.compare_digest(expected, provided)


async def _request_args(request):
	args = dict(request.query_params)
	if request.method == "POST":
		content_type = request.headers.get("content-type", "")
		if "application/json" in content_type:
			try:
				body = await request.json()
			except ValueError:
				body = None
			if isinstance(body, dict):
				args.update(body)
		elif "form" in content_type:
			form = await request.form()
			args.update({key: value for key, value in form.items() if isinstance(value, str)})
	return args


@app.api_route("/api/method/{method}", methods=["GET", "POST"], include_in_schema=False)
async def api_method(request: Request, method: str):
	func = METHODS.get(method)
	if not func:
		return JSONResponse(_error_payload(f"Method {method} not found", "DoesNotExistError"), status_code=404)
	if request.method not in func._http_methods:
		return JSONResponse(_error_payload("Method not allowed", "PermissionError"), status_code=405)
	args = await _request_args(request)
	args.pop("cmd", None)

	db = SessionLocal()
	try:
		ctx = _context(request, db)
		if not func._allow_guest:
			if ctx.is_guest:
				return JSONResponse(_error_payload("Please sign in to continue.", "AuthenticationError"), status_code=401)
			if request.method == "POST" and not _csrf_ok(request):
				return JSONResponse(
					_error_payload("Your session has expired. Refresh the page and try again.", "CSRFTokenError"), status_code=400
				)
		result = call(func, ctx, args)
		db.commit()
		return JSONResponse({"message": result}, headers={"Cache-Control": "no-store"})
	except AppError as error:
		db.rollback()
		return JSONResponse(_error_payload(error.message, error.exc_type), status_code=error.status_code)
	except Exception:
		db.rollback()
		log.exception("Error in %s", method)
		return JSONResponse(_error_payload("Something went wrong. Please try again.", "Exception"), status_code=500)
	finally:
		db.close()


# --------------------------------------------------------------------- cron


@app.get("/api/cron/expire-trials", include_in_schema=False)
def cron_expire_trials(request: Request):
	expected = settings.cron_secret
	if not expected or not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {expected}"):
		raise HTTPException(status_code=401)
	with session_scope() as db:
		return {"expired": accounts.expire_trials(db)}


@app.get("/api/health", include_in_schema=False)
def health():
	with SessionLocal() as db:
		db.execute(select(SignupDetails.id).limit(1))
	return {"status": "ok"}
