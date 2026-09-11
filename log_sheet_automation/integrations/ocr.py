# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""OCR provider abstraction.

Three providers, selected by `Log Sheet Automation Settings.ocr_provider_mode`:

- **Mock** — returns fixed canned values, exactly as the original Desk-UI
  prototype did. Used for demos; requires no credentials.
- **Azure** — calls a deployed Azure AI Document Intelligence *custom*
  extraction model over its REST API (analyze -> poll -> parse). This is
  real, working integration code, but it has not been exercised against a
  live Azure endpoint in this environment (no credentials were available
  when it was written) — verify it against your actual deployed model
  before relying on it for a go-live. In particular: your model must
  return numeric fields named exactly `working_hours`, `idle_hours`,
  `standby_hours`, `breakdown_hours` (train/label the model accordingly,
  or adjust FIELD_NAMES below to match your model's actual field names).
- **Google Document AI** — calls a deployed Google Cloud Document AI
  processor (a trained/custom extractor) via its REST `:process` endpoint,
  authenticating as the service account whose key JSON is pasted into
  `google_service_account_key` in Settings. Same caveat as Azure: real,
  working code, but not exercised against a live processor in this
  environment, and your processor's entity *type* names must match
  FIELD_NAMES below (or you adjust FIELD_NAMES to match your schema).

  SECURITY NOTE ON THE SERVICE ACCOUNT KEY: this module always reads the
  key from `Log Sheet Automation Settings` (a Password-type field, stored
  encrypted in this site's own database via Frappe's `get_password`) — it
  is never hardcoded here or committed to source control. Paste your key
  into that field from the Desk UI after install; don't put a live key in
  a file that ends up in git history. If a service account key was ever
  shared somewhere it shouldn't have been (chat, a public repo, a ticket),
  treat it as compromised and rotate/delete it in Google Cloud Console
  (IAM & Admin -> Service Accounts -> Keys) regardless of whether it was
  actually used.

All three providers return the same shape from `extract()`, so
`api.run_log_sheet_ocr` and the rest of the workflow never need to know
which one ran.
"""

import time

import frappe
from frappe.utils import now_datetime

FIELD_NAMES = ["working_hours", "idle_hours", "standby_hours", "breakdown_hours"]

# How long to poll an Azure analyze operation before giving up.
AZURE_POLL_INTERVAL_SECONDS = 2
AZURE_POLL_MAX_ATTEMPTS = 30  # ~60s total, overridable via ocr_timeout_seconds


def extract(doc, settings):
	"""Returns (run_id, rows) where rows is a list of
	{"field", "value", "confidence"} dicts, one per FIELD_NAMES entry.
	Raises frappe.ValidationError on a hard provider failure (Mock never
	fails; Azure and Google Document AI can).
	"""
	if settings.ocr_provider_mode == "Azure":
		return _extract_azure(doc, settings)
	if settings.ocr_provider_mode == "Google Document AI":
		return _extract_google(doc, settings)
	return _extract_mock()


def _extract_mock():
	run_id = f"OCR-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	rows = [
		{"field": "working_hours", "value": 8.0, "confidence": 0.98},
		{"field": "idle_hours", "value": 0.5, "confidence": 0.95},
		{"field": "standby_hours", "value": 0.3, "confidence": 0.92},
		{"field": "breakdown_hours", "value": 1.5, "confidence": 0.72},
	]
	return run_id, rows


def _extract_azure(doc, settings):
	import requests

	if not doc.source_document:
		frappe.throw(frappe._("Attach the scanned/photographed log sheet (Source Document) before running Azure OCR."))
	if not settings.azure_endpoint or not settings.get_password("azure_api_key", raise_exception=False):
		frappe.throw(frappe._("Azure Document Intelligence Endpoint and API Key must be set in Log Sheet Automation Settings."))

	endpoint = settings.azure_endpoint.rstrip("/")
	api_key = settings.get_password("azure_api_key")
	model_id = settings.azure_model_id or "log-sheet-v1"
	api_version = settings.azure_api_version or "2024-02-29-preview"
	timeout = frappe.utils.cint(settings.ocr_timeout_seconds) or 30

	file_doc = frappe.get_doc("File", {"file_url": doc.source_document})
	content = file_doc.get_content()
	content_type = _guess_content_type(file_doc.file_name or doc.source_document)

	analyze_url = f"{endpoint}/documentintelligence/documentModels/{model_id}:analyze?api-version={api_version}"
	headers = {"Ocp-Apim-Subscription-Key": api_key, "Content-Type": content_type}

	response = requests.post(analyze_url, headers=headers, data=content, timeout=timeout)
	if response.status_code != 202:
		frappe.throw(
			frappe._("Azure Document Intelligence rejected the analyze request ({0}): {1}").format(
				response.status_code, response.text[:500]
			)
		)

	operation_location = response.headers.get("Operation-Location")
	if not operation_location:
		frappe.throw(frappe._("Azure Document Intelligence did not return an Operation-Location header."))

	result = _poll_azure_operation(operation_location, api_key, timeout)

	documents = (result.get("analyzeResult") or {}).get("documents") or []
	if not documents:
		frappe.throw(frappe._("Azure Document Intelligence returned no documents for this file."))

	fields = documents[0].get("fields") or {}
	run_id = f"OCR-AZURE-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	rows = []
	for field_name in FIELD_NAMES:
		field_result = fields.get(field_name) or {}
		value = field_result.get("valueNumber")
		confidence = field_result.get("confidence")
		if value is None:
			# Field wasn't found/extracted on this document — record it as a
			# zero-confidence miss rather than silently dropping it, so it
			# surfaces in AI Review like any other low-confidence field.
			value = 0.0
			confidence = 0.0
		rows.append({"field": field_name, "value": value, "confidence": confidence})

	return run_id, rows


def _poll_azure_operation(operation_location, api_key, timeout_seconds):
	import requests

	headers = {"Ocp-Apim-Subscription-Key": api_key}
	attempts = min(AZURE_POLL_MAX_ATTEMPTS, max(1, timeout_seconds // AZURE_POLL_INTERVAL_SECONDS))

	for _attempt in range(attempts):
		poll_response = requests.get(operation_location, headers=headers, timeout=timeout_seconds)
		poll_response.raise_for_status()
		result = poll_response.json()
		status = result.get("status")
		if status == "succeeded":
			return result
		if status == "failed":
			frappe.throw(frappe._("Azure Document Intelligence analysis failed: {0}").format(result.get("error")))
		time.sleep(AZURE_POLL_INTERVAL_SECONDS)

	frappe.throw(frappe._("Azure Document Intelligence analysis did not complete within {0}s.").format(timeout_seconds))


def _guess_content_type(filename):
	filename = (filename or "").lower()
	if filename.endswith(".pdf"):
		return "application/pdf"
	if filename.endswith(".png"):
		return "image/png"
	if filename.endswith((".jpg", ".jpeg")):
		return "image/jpeg"
	if filename.endswith(".tif") or filename.endswith(".tiff"):
		return "image/tiff"
	return "application/octet-stream"


# ---------------------------------------------------------------------------
# Google Document AI
# ---------------------------------------------------------------------------

def _extract_google(doc, settings):
	import base64
	import json as json_module

	import requests

	if not doc.source_document:
		frappe.throw(
			frappe._("Attach the scanned/photographed log sheet (Source Document) before running Google Document AI OCR.")
		)

	project_id = settings.google_project_id
	location = settings.google_location or "us"
	processor_id = settings.google_processor_id
	key_json_text = settings.get_password("google_service_account_key", raise_exception=False)

	if not (project_id and processor_id and key_json_text):
		frappe.throw(
			frappe._(
				"Google Cloud Project ID, Document AI Processor ID, and Service Account Key must all be set "
				"in Log Sheet Automation Settings."
			)
		)

	try:
		service_account_info = json_module.loads(key_json_text)
	except ValueError:
		frappe.throw(
			frappe._(
				"The stored Google Service Account Key is not valid JSON. Re-paste the full contents of the "
				"key file into Settings."
			)
		)

	access_token = _get_google_access_token(service_account_info)

	file_doc = frappe.get_doc("File", {"file_url": doc.source_document})
	content = file_doc.get_content()
	mime_type = _guess_content_type(file_doc.file_name or doc.source_document)
	timeout = frappe.utils.cint(settings.ocr_timeout_seconds) or 30

	process_url = (
		f"https://{location}-documentai.googleapis.com/v1/projects/{project_id}"
		f"/locations/{location}/processors/{processor_id}:process"
	)
	headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
	payload = {"rawDocument": {"content": base64.b64encode(content).decode("ascii"), "mimeType": mime_type}}

	response = requests.post(process_url, headers=headers, json=payload, timeout=timeout)
	if response.status_code != 200:
		frappe.throw(
			frappe._("Google Document AI rejected the process request ({0}): {1}").format(
				response.status_code, response.text[:500]
			)
		)

	result = response.json()
	entities = (result.get("document") or {}).get("entities") or []
	# First entity wins per type — a custom extractor may return more than
	# one mention of the same field type on a noisy scan.
	by_type = {}
	for entity in entities:
		entity_type = entity.get("type")
		if entity_type and entity_type not in by_type:
			by_type[entity_type] = entity

	run_id = f"OCR-GOOGLE-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	rows = []
	for field_name in FIELD_NAMES:
		entity = by_type.get(field_name)
		if entity is None:
			# Entity type not present in this processor's output — record it
			# as a zero-confidence miss rather than silently dropping it, so
			# it surfaces in AI Review like any other low-confidence field.
			rows.append({"field": field_name, "value": 0.0, "confidence": 0.0})
			continue
		raw_value = (entity.get("normalizedValue") or {}).get("text") or entity.get("mentionText") or "0"
		try:
			value = float(str(raw_value).strip())
		except ValueError:
			value = 0.0
		rows.append({"field": field_name, "value": value, "confidence": entity.get("confidence") or 0.0})

	return run_id, rows


def _get_google_access_token(service_account_info):
	"""Exchanges the service account key for a short-lived OAuth2 access
	token. Uses the `google-auth` package (add it to the bench's Python
	environment — it is not part of a stock Frappe install)."""
	try:
		from google.auth.transport.requests import Request as GoogleAuthRequest
		from google.oauth2 import service_account as google_service_account
	except ImportError:
		frappe.throw(
			frappe._(
				"The 'google-auth' Python package is required for Google Document AI OCR. Install it in "
				"this site's bench environment, e.g.: ./env/bin/pip install google-auth"
			)
		)

	credentials = google_service_account.Credentials.from_service_account_info(
		service_account_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
	)
	credentials.refresh(GoogleAuthRequest())
	return credentials.token
