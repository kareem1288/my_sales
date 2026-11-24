import json
import os
import re
import sys
from pathlib import Path

import pytest

DB_PATH = Path(__file__).parent / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["CRON_SECRET"] = "cron-secret"
if DB_PATH.exists():
	DB_PATH.unlink()
sys.argv = ["cli", "seed-demo", "--days", "20"]

from fastapi.testclient import TestClient  # noqa: E402

from app import cli  # noqa: E402
from app.main import app  # noqa: E402

cli.main()

SIGNUP = {
	"full_name": "Ravi Kumar", "email_address": "ravi@example.com", "phone_number": "9876543210",
	"password": "Secret@123", "store_name": "Ravi Kirana", "business_type": "Grocery Store",
	"store_address": "1 Main Road", "city": "Hyderabad", "pin_code": "500001", "state": "Telangana",
	"country": "India", "currency": "INR", "financial_year_start": "1 April", "tax_preference": "GST", "language": "en",
}


def login(client, email="demo@example.com", password="Demo@1234"):
	from app.db import session_scope
	from app.models import RateLimit

	with session_scope() as db:  # tests log in far more often than the limiter allows
		db.query(RateLimit).delete()
	response = client.post("/api/method/store_management.otp_auth.password_login", json={"email": email, "password": password})
	assert response.status_code == 200, response.text
	page = client.get("/masters")
	token = re.search(r'"csrf_token": "([^"]+)"', page.text).group(1)
	client.headers["X-Frappe-CSRF-Token"] = token
	return client


def api(client, method, expect=200, **args):
	response = client.post(f"/api/method/{method}", json=args)
	assert response.status_code == expect, response.text
	return response.json().get("message")


def error_text(response):
	return " ".join(json.loads(m)["message"] for m in json.loads(response.json()["_server_messages"]))


@pytest.fixture
def client():
	with TestClient(app) as c:
		yield c


@pytest.fixture
def demo(client):
	return login(client)


def test_guest_redirects_and_login_errors(client):
	assert client.get("/pos", follow_redirects=False).headers["location"].startswith("/store-login")
	assert client.get("/store-login").status_code == 200
	bad = client.post("/api/method/store_management.otp_auth.password_login", json={"email": "demo@example.com", "password": "nope"})
	assert bad.status_code == 401 and "Invalid login" in error_text(bad)
	assert client.post("/api/method/store_management.api.get_pos_bootstrap").status_code == 401


def test_pages_render(demo):
	for path in ["/pos", "/masters", "/reports?type=sales", "/assistant", "/more"]:
		response = demo.get(path)
		assert response.status_code == 200, path
		assert "Demo Company" in response.text or path in {"/assistant", "/more", "/masters"}
	assert demo.get("/store-login", follow_redirects=False).status_code == 302
	assert demo.get("/api/method/store_management.api.get_brand_asset?name=my-sales-logo.svg").status_code == 200
	assert demo.get("/api/method/store_management.api.get_brand_asset?name=../../config.py").status_code == 404
	assert demo.get("/assets/store_management/js/reports.js").status_code == 200


def test_csrf_required(demo):
	del demo.headers["X-Frappe-CSRF-Token"]
	response = demo.post("/api/method/store_management.api.get_pos_bootstrap")
	assert response.status_code == 400


def test_bootstrap_and_dashboard(demo):
	boot = api(demo, "store_management.api.get_pos_bootstrap")
	assert boot["company"] == "Demo Company" and len(boot["items"]) == 10 and boot["categories"]
	summary = api(demo, "store_management.api.get_dashboard_summary", from_date="2000-01-01", to_date="2000-01-31")
	assert summary["period_sales"] == 0 and len(summary["trend"]) == 31
	summary = api(demo, "store_management.api.get_dashboard_summary")
	assert summary["period_bills"] > 0 and len(summary["current_summaries"]["week"]["rows"]) == 7
	bad = demo.post("/api/method/store_management.api.get_dashboard_summary", json={"from_date": "2026-02-01", "to_date": "2026-01-01"})
	assert bad.status_code == 417


def test_barcode_lookup(demo):
	rows = api(demo, "store_management.api.get_pos_items_by_barcode", query="8901000000011")
	assert [r["name"] for r in rows] == ["DEMO-RICE-5KG"]


def test_create_bill_cash_and_split(demo):
	before = api(demo, "store_management.api.get_master_record", doctype="Item", name="DEMO-SUGAR-1KG")["stock_qty"]
	bill = api(demo, "store_management.api.create_pos_bill", payment_method="Cash", additional_discount_amount=3.5,
		items=[{"item_code": "DEMO-SUGAR-1KG", "qty": 2, "rate": 52.25}, {"item_code": "DEMO-TEA-250G", "qty": 1, "rate": 140}])
	assert bill["grand_total"] == 241.0 and bill["rounded_total"] == 241.0 and bill["payment_entry"]
	after = api(demo, "store_management.api.get_master_record", doctype="Item", name="DEMO-SUGAR-1KG")["stock_qty"]
	assert after == before - 2

	receipt = demo.get(f"/printview?doctype=Sales%20Invoice&name={bill['name']}")
	assert receipt.status_code == 200 and bill["name"] in receipt.text and "Sugar 1 kg" in receipt.text

	split = api(demo, "store_management.api.create_pos_bill", payment_method="Split",
		items=[{"item_code": "DEMO-OIL-1L", "qty": 1, "rate": 155}],
		split_payments=[{"method": "Cash", "amount": 100}, {"method": "Card", "amount": 55}])
	assert len(split["payment_entries"]) == 2

	wrong = demo.post("/api/method/store_management.api.create_pos_bill", json={
		"payment_method": "Split", "items": [{"item_code": "DEMO-OIL-1L", "qty": 1, "rate": 155}],
		"split_payments": [{"method": "Cash", "amount": 100}, {"method": "Card", "amount": 10}]})
	assert wrong.status_code == 417 and "add up" in error_text(wrong)
	empty = demo.post("/api/method/store_management.api.create_pos_bill", json={"items": []})
	assert empty.status_code == 417
	unknown = demo.post("/api/method/store_management.api.create_pos_bill", json={"items": [{"item_code": "NOPE", "qty": 1, "rate": 5}]})
	assert unknown.status_code == 417


def test_upi_test_mode(demo):
	upi = api(demo, "store_management.api.create_upi_payment", amount=99)
	assert upi["mode"] == "Testing" and upi["chargeable"] is False and upi["qr_code"].startswith("data:image/svg+xml")


@pytest.mark.parametrize("report_type", ["sales", "items", "customers", "open-bills", "profit-and-loss", "general-ledger", "trial-balance"])
def test_reports(demo, report_type):
	data = api(demo, "store_management.api.run_my_sales_report", report_type=report_type, filters={"company": "Demo Company"})
	assert data["columns"]
	if report_type != "open-bills":
		assert data["result"], report_type


def test_trial_balance_balances(demo):
	rows = api(demo, "store_management.api.run_my_sales_report", report_type="trial-balance", filters={"from_date": "2000-01-01"})["result"]
	assert abs(sum(r["debit"] for r in rows) - sum(r["credit"] for r in rows)) < 0.01


def test_report_scripts_and_sidebar(demo):
	script = api(demo, "frappe.desk.query_report.get_script", report_name="Sales Register")
	assert any(f["fieldname"] == "from_date" for f in script["filters"])
	sidebar = api(demo, "store_management.api.get_report_sidebar", include_catalog=1)
	assert len(sidebar["reports"]) == 7 and {r["name"] for r in sidebar["catalog"]} >= {"Payment Summary"}
	added = api(demo, "store_management.api.add_report_to_sidebar", report_name="Payment Summary")
	route = next(r["route_key"] for r in added["reports"] if r["report_name"] == "Payment Summary")
	assert api(demo, "store_management.api.run_my_sales_report", report_type=route)["result"]
	api(demo, "store_management.api.set_report_sidebar_visibility", report_name="Payment Summary", visible=0)
	api(demo, "store_management.api.remove_report_from_sidebar", report_name="Payment Summary")
	blocked = demo.post("/api/method/store_management.api.remove_report_from_sidebar", json={"report_name": "Sales Register"})
	assert blocked.status_code == 417


def test_masters_crud(demo):
	api(demo, "store_management.api.create_master_record", doctype="Item Group", item_group_name="Dairy")
	api(demo, "store_management.api.create_master_record", doctype="Item", item_code="MILK-1L", item_name="Milk 1 L",
		item_group="Dairy", stock_uom="Litre", standard_rate="60", opening_stock="20", is_stock_item=1)
	item = api(demo, "store_management.api.get_master_record", doctype="Item", name="MILK-1L")
	assert item["stock_qty"] == 20 and item["standard_rate"] == 60
	api(demo, "store_management.api.update_master_record", doctype="Item", name="MILK-1L", standard_rate="62", disabled=1)
	assert api(demo, "store_management.api.get_master_record", doctype="Item", name="MILK-1L")["disabled"] == 1
	dup = demo.post("/api/method/store_management.api.create_master_record", json={"doctype": "Item", "item_code": "MILK-1L",
		"item_name": "x", "item_group": "Dairy", "stock_uom": "Nos"})
	assert dup.status_code == 417
	in_use = demo.post("/api/method/store_management.api.delete_master_record", json={"doctype": "Item Group", "name": "Dairy"})
	assert in_use.status_code == 417
	api(demo, "store_management.api.delete_master_record", doctype="Item", name="MILK-1L")
	api(demo, "store_management.api.delete_master_record", doctype="Item Group", name="Dairy")
	used = demo.post("/api/method/store_management.api.delete_master_record", json={"doctype": "Item", "name": "DEMO-RICE-5KG"})
	assert used.status_code == 417

	api(demo, "store_management.api.create_master_record", doctype="Item Tax Template", title="GST 5%", company="Demo Company")
	assert any(r["name"] == "GST 5% - DEMO" for r in api(demo, "store_management.api.get_master_records", doctype="Item Tax Template"))
	expense = api(demo, "store_management.api.create_master_record", doctype="Expense", posting_date="2026-11-02",
		description="Shop rent", amount="15000")
	assert expense["name"].startswith("EXP-2026-")
	report = api(demo, "store_management.api.run_my_sales_report", report_type="expenses",
		filters={"from_date": "2026-11-01", "to_date": "2026-11-30"})
	assert any(r["description"] == "Shop rent" and r["amount"] == 15000 for r in report["result"])
	bad = demo.post("/api/method/store_management.api.create_master_record", json={"doctype": "Expense",
		"posting_date": "2026-11-02", "description": "x", "amount": "-5"})
	assert bad.status_code == 417
	links = api(demo, "store_management.api.get_master_link_records", doctype="Currency")
	assert {"name": "INR"} in links
	forbidden = demo.post("/api/method/store_management.api.get_master_records", json={"doctype": "Sales Invoice"})
	assert forbidden.status_code == 403


def test_user_management_and_cashier_permissions(client):
	demo = login(client)
	api(demo, "store_management.api.create_master_record", doctype="User", email="cashier@example.com",
		first_name="Cash", role_profiles="Cashier", new_password="Cashier@123")
	users = api(demo, "store_management.api.get_master_records", doctype="User")
	assert all("password_hash" not in u for u in users)
	demo.get("/logout")

	cashier = login(client, "cashier@example.com", "Cashier@123")
	assert api(cashier, "store_management.api.create_master_record", doctype="Customer", customer_name="Till Customer")
	denied = cashier.post("/api/method/store_management.api.create_master_record",
		json={"doctype": "Item Group", "item_group_name": "Nope"})
	assert denied.status_code == 403


def test_signup_isolated_store_and_trial(client):
	result = api(client, "store_management.signup.create_signup_details", data=SIGNUP)
	assert result["provisioned"]
	duplicate = client.post("/api/method/store_management.signup.create_signup_details", json={"data": SIGNUP})
	assert duplicate.status_code == 417

	ravi = login(client, "ravi@example.com", "Secret@123")
	boot = api(ravi, "store_management.api.get_pos_bootstrap")
	assert boot["company"] == "Ravi Kirana" and boot["items"] == []
	assert api(ravi, "store_management.api.get_dashboard_summary")["stores"] == ["Ravi Kirana"]
	# Another store's data must be invisible.
	assert ravi.post("/api/method/store_management.api.get_master_record", json={"doctype": "Item", "name": "DEMO-RICE-5KG"}).status_code == 404
	other = ravi.post("/api/method/store_management.api.get_dashboard_summary", json={"company": "Demo Company"})
	assert other.status_code == 417
	sales = api(ravi, "store_management.api.run_my_sales_report", report_type="sales")
	assert sales["result"] == []
	assert ravi.get("/printview?name=SINV-2026-00001").status_code == 404
	ravi.get("/logout")

	# Expire the trial: login must now be refused with the contact message.
	from app.db import session_scope
	from app.models import SignupDetails
	from app.utils import add_days, today

	with session_scope() as db:
		db.query(SignupDetails).filter_by(email_address="ravi@example.com").update({"trial_end_date": add_days(today(), -1)})
	expired = client.post("/api/method/store_management.otp_auth.password_login", json={"email": "ravi@example.com", "password": "Secret@123"})
	assert expired.status_code == 401 and "trial has ended" in error_text(expired)
	status = client.get("/api/method/store_management.signup.get_trial_login_status?email=ravi@example.com").json()["message"]
	assert status["expired"] is True


def test_upload_and_file_access(client):
	demo = login(client)
	png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
	uploaded = demo.post("/api/method/upload_file", files={"file": ("pic.png", png, "image/png")})
	assert uploaded.status_code == 200, uploaded.text
	url = uploaded.json()["message"]["file_url"]
	assert demo.get(url).content == png
	not_image = demo.post("/api/method/upload_file", files={"file": ("x.html", b"<script>", "text/html")})
	assert not_image.status_code == 417
	demo.get("/logout")
	assert client.get(url).status_code == 404


def test_assistant_shortcuts(demo):
	reply = api(demo, "store_management.assistant.ask", question="Top 5 selling items")
	assert "Top selling" in reply["text"] or "No sales" in reply["text"]
	reply = api(demo, "store_management.assistant.ask", question="What is the capital of France?")
	assert "MSA_ENABLED" in reply["text"]


def test_cron_requires_secret(client):
	assert client.get("/api/cron/expire-trials").status_code == 401
	assert client.get("/api/cron/expire-trials", headers={"Authorization": "Bearer cron-secret"}).status_code == 200
