# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""SAP validation provider abstraction.

Two providers, selected by `Log Sheet Automation Settings.sap_provider_mode`:

- **Mock** — the five canned checks (SO_DATE, OPEN_QTY, EQUIPMENT_MATCH,
  WO_REF, RATE) from the original prototype, with `mock_sap_scenario`
  letting a demo force an EXPIRED_SO / INSUFFICIENT_QTY exception on
  demand.
- **Live** — POSTs a validation request to `sap_endpoint` and expects a
  JSON response shaped like `{"checks": [{"code", "status", "message"}]}`
  (status one of Passed/Exception/Failed). This is a *generic* REST
  contract, not any specific SAP module's real API — real SAP landscapes
  expose this kind of check through very different mechanisms (an OData
  service, SAP PI/PO, an RFC-fronting middleware, a BTP integration
  flow...). Treat `_call_live_endpoint()` below as the one function you
  replace with whatever your actual SAP integration layer expects; the
  rest of this module (and the rest of the app) only depends on getting
  back that `checks` list, not on how it was obtained. Not live-tested
  against a real SAP endpoint in this environment.
"""

import frappe
from frappe.utils import now_datetime

MOCK_CHECKS = [
	{"code": "SO_DATE", "status": "Passed", "message": "Sales order within valid date range."},
	{"code": "OPEN_QTY", "status": "Passed", "message": "Sufficient open quantity."},
	{"code": "EQUIPMENT_MATCH", "status": "Passed", "message": "Equipment matched to mapped site."},
	{"code": "WO_REF", "status": "Passed", "message": "Work order reference valid."},
	{"code": "RATE", "status": "Passed", "message": "Rate available."},
]


def validate(doc, settings):
	"""Returns (run_id, checks) where checks is a list of
	{"code", "status", "message"} dicts. Status is one of
	Passed / Exception / Failed.
	"""
	if settings.sap_provider_mode == "Live":
		return _validate_live(doc, settings)
	return _validate_mock(settings)


def _validate_mock(settings):
	run_id = f"SAP-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	checks = [dict(c) for c in MOCK_CHECKS]  # copy — caller may mutate

	scenario = settings.mock_sap_scenario or "PASS"
	if scenario == "EXPIRED_SO":
		checks[0].update(status="Exception", message="Sales order validity has expired.")
	elif scenario == "INSUFFICIENT_QTY":
		checks[1].update(status="Exception", message="Insufficient open quantity on sales order item.")

	return run_id, checks


def _validate_live(doc, settings):
	run_id = f"SAP-LIVE-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	try:
		checks = _call_live_endpoint(doc, settings)
	except Exception:
		frappe.log_error(title="Log Sheet Automation: live SAP validation failed", message=frappe.get_traceback())
		# Fail closed: a validation Failed status blocks the Operations gate
		# (BR-010) rather than silently letting a log sheet through when the
		# real SAP system couldn't be reached.
		checks = [{"code": "SAP_ENDPOINT", "status": "Failed", "message": "Could not reach the configured SAP endpoint. See error log."}]
	return run_id, checks


def _call_live_endpoint(doc, settings):
	"""Replace this function's body with whatever your SAP integration
	layer actually expects. As shipped it implements a plain REST contract:
	POST {sap_endpoint} with a JSON payload, expecting back
	{"checks": [{"code", "status", "message"}, ...]}.
	"""
	import requests

	if not settings.sap_endpoint:
		frappe.throw(frappe._("SAP Endpoint (Live) is not set in Log Sheet Automation Settings."))

	headers = {"Content-Type": "application/json"}
	if settings.sap_auth_type == "API Key":
		credential = settings.get_password("sap_credential", raise_exception=False)
		if credential:
			headers["Authorization"] = f"ApiKey {credential}"
	elif settings.sap_auth_type == "OAuth":
		# `sap_credential` is expected to hold a pre-obtained bearer token.
		# If your SAP system requires a full OAuth2 client-credentials
		# exchange, do that here (or in a scheduled job that refreshes a
		# cached token) rather than storing a long-lived token in Settings.
		token = settings.get_password("sap_credential", raise_exception=False)
		if token:
			headers["Authorization"] = f"Bearer {token}"

	payload = {
		"log_sheet": doc.name,
		"operating_site": doc.operating_site,
		"equipment": doc.equipment,
		"log_date": str(doc.log_date),
		"sap_sales_order": doc.sap_sales_order,
		"sap_sales_order_item": doc.sap_sales_order_item,
		"work_order_reference": doc.work_order_reference,
	}

	timeout = frappe.utils.cint(settings.sap_timeout_seconds) or 30
	response = requests.post(settings.sap_endpoint, json=payload, headers=headers, timeout=timeout)
	response.raise_for_status()
	body = response.json()

	checks = body.get("checks")
	if not isinstance(checks, list) or not checks:
		frappe.throw(frappe._("SAP endpoint response did not include a non-empty 'checks' list."))
	return checks
