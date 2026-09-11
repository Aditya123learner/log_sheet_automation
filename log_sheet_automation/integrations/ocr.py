# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

import re
import time

import frappe
from frappe.utils import now_datetime

FIELD_NAMES = ["working_hours", "idle_hours", "standby_hours", "breakdown_hours"]

# Best-effort label variants to look for in the raw text Vision API returns,
# tried in order for each field until one matches. UNVERIFIED against a real
# log sheet — this is only the fallback used when Settings.
# ocr_field_label_patterns doesn't override a given field (see
# _get_field_label_patterns below, and the module docstring above). Matches
# "<label> <optional : or -> <number>", case-insensitive.
DEFAULT_FIELD_LABEL_PATTERNS = {
	"working_hours": [r"working\s*hours?", r"work\s*hrs?\.?", r"w\.?\s*hrs?\.?"],
	"idle_hours": [r"idle\s*hours?", r"idle\s*hrs?\.?"],
	"standby_hours": [r"stand\s*-?\s*by\s*hours?", r"standby\s*hrs?\.?"],
	"breakdown_hours": [r"break\s*-?\s*down\s*hours?", r"breakdown\s*hrs?\.?", r"b\.?d\.?\s*hrs?\.?"],
}

# How long to poll an Azure analyze operation before giving up.
AZURE_POLL_INTERVAL_SECONDS = 2
AZURE_POLL_MAX_ATTEMPTS = 30  # ~60s total, overridable via ocr_timeout_seconds


def extract(doc, settings):
	"""Returns (run_id, rows, meta) — rows is a list of
	{"field", "value", "confidence"} dicts, one per FIELD_NAMES entry; meta
	is a dict of extra, provider-specific info for the audit trail (for
	Google Vision: {"raw_text": <what Vision actually recognized>}, so you
	can see exactly what to tune ocr_field_label_patterns against — empty
	for Mock/Azure). Raises frappe.ValidationError on a hard provider
	failure (Mock never fails; Azure and Google Vision can).
	"""
	if settings.ocr_provider_mode == "Azure":
		return _extract_azure(doc, settings)
	if settings.ocr_provider_mode == "Google Vision API":
		return _extract_google_vision(doc, settings)
	return _extract_mock()


def _extract_mock():
	run_id = f"OCR-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	rows = [
		{"field": "working_hours", "value": 8.0, "confidence": 0.98},
		{"field": "idle_hours", "value": 0.5, "confidence": 0.95},
		{"field": "standby_hours", "value": 0.3, "confidence": 0.92},
		{"field": "breakdown_hours", "value": 1.5, "confidence": 0.72},
	]
	return run_id, rows, {}


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

	return run_id, rows, {}


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
# Google Vision API
# ---------------------------------------------------------------------------

def _extract_google_vision(doc, settings):
	import base64
	import json as json_module

	import requests

	if not doc.source_document:
		frappe.throw(
			frappe._("Attach the scanned/photographed log sheet (Source Document) before running Google Vision OCR.")
		)

	key_json_text = settings.get_password("google_service_account_key", raise_exception=False)
	if not key_json_text:
		frappe.throw(frappe._("Google Service Account Key (JSON) must be set in Log Sheet Automation Settings."))

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
	timeout = frappe.utils.cint(settings.ocr_timeout_seconds) or 30

	annotate_url = "https://vision.googleapis.com/v1/images:annotate"
	headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
	payload = {
		"requests": [
			{
				"image": {"content": base64.b64encode(content).decode("ascii")},
				"features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
			}
		]
	}

	response = requests.post(annotate_url, headers=headers, json=payload, timeout=timeout)
	if response.status_code != 200:
		frappe.throw(
			frappe._("Google Vision API rejected the request ({0}): {1}").format(
				response.status_code, response.text[:500]
			)
		)

	result = response.json()
	api_response = (result.get("responses") or [{}])[0]
	if api_response.get("error"):
		frappe.throw(frappe._("Google Vision API returned an error: {0}").format(api_response["error"].get("message")))

	full_text = (api_response.get("fullTextAnnotation") or {}).get("text") or ""
	if not full_text.strip():
		frappe.throw(frappe._("Google Vision API returned no readable text for this file — check the scan quality."))

	patterns_by_field = _get_field_label_patterns(settings)

	run_id = f"OCR-GOOGLEVISION-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	rows = []
	for field_name in FIELD_NAMES:
		value, confidence = _match_field_in_text(full_text, patterns_by_field.get(field_name, []))
		rows.append({"field": field_name, "value": value, "confidence": confidence})

	# Truncated so a very noisy scan doesn't blow out the audit trail —
	# still long enough to see the label wording that actually needs
	# matching. Full text is in the API response itself if you need more.
	meta = {"raw_text": full_text[:1000]}
	return run_id, rows, meta


def _get_field_label_patterns(settings):
	"""Merges Settings.ocr_field_label_patterns (a JSON object of
	{field_name: [pattern, ...]}, edited from the Desk UI — no code deploy
	needed) over DEFAULT_FIELD_LABEL_PATTERNS. A field the JSON doesn't
	mention, or an empty/blank setting, falls back to the default for that
	field. Malformed JSON is a hard error (not silently ignored) so a typo
	doesn't quietly revert to defaults without anyone noticing."""
	import json as json_module

	raw = (settings.ocr_field_label_patterns or "").strip()
	if not raw:
		return dict(DEFAULT_FIELD_LABEL_PATTERNS)

	try:
		overrides = json_module.loads(raw)
	except ValueError as e:
		frappe.throw(
			frappe._(
				"Log Sheet Automation Settings > OCR Field Label Patterns is not valid JSON: {0}"
			).format(e)
		)

	if not isinstance(overrides, dict):
		frappe.throw(frappe._("OCR Field Label Patterns must be a JSON object of {\"field_name\": [\"pattern\", ...]}."))

	merged = dict(DEFAULT_FIELD_LABEL_PATTERNS)
	for field_name, patterns in overrides.items():
		if isinstance(patterns, list) and patterns:
			merged[field_name] = patterns
	return merged


def _match_field_in_text(full_text, label_patterns):
	"""Looks for one of label_patterns in the raw OCR text, immediately
	followed by a number. Returns (value, confidence) — confidence here is
	NOT something Google gave us (Vision doesn't score per-field like
	Document AI does); it's just 0.9 if a label+number match was found,
	0.0 if it wasn't, so AI Review still flags misses the same way it
	flags a low-confidence Azure/Document AI field."""
	for label_pattern in label_patterns:
		match = re.search(label_pattern + r"\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)", full_text, re.IGNORECASE)
		if match:
			try:
				return float(match.group(1)), 0.9
			except ValueError:
				continue
	return 0.0, 0.0


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
				"The 'google-auth' Python package is required for Google Vision OCR. Install it in "
				"this site's bench environment, e.g.: ./env/bin/pip install google-auth"
			)
		)

	credentials = google_service_account.Credentials.from_service_account_info(
		service_account_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
	)
	credentials.refresh(GoogleAuthRequest())
	return credentials.token
