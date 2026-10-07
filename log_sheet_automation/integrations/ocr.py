# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""OCR provider — Google Cloud Vision.

`Log Sheet Automation Settings.ocr_provider_mode` is locked to **Google
Vision API**. This module is the thin, Frappe-aware layer around two plain
Python helpers:

- `ocr_pages.py` turns the attached Source Document (photo, image, or a
  multi-page scanned PDF) into one small JPEG per page — Vision's
  `images:annotate` accepts only images and only up to ~10 MB per request,
  while the real log sheets arrive as multi-page scanner PDFs of 20 MB+.
- `ocr_layout.py` rebuilds the Daily Log table and the printed header
  block from the word POSITIONS Vision returns (Vision has no notion of a
  table; the earlier line-by-line regular expression could never match a
  ruled handwritten form and produced zero rows).

What this module itself does: read the file, get a Google access token from
the service account key stored in Settings, call Vision once per page, and
hand back one result per page. `api.run_log_sheet_ocr` then writes page 1
onto the sheet OCR was run from and creates one more Equipment Log Sheet for
every further page (one physical sheet per PDF page).

SECURITY NOTE ON THE SERVICE ACCOUNT KEY: the key is always read from
`Log Sheet Automation Settings` (uploaded there as the .json file from
Google Cloud, then kept in a Password-type field, stored encrypted in this
site's own database; the uploaded file itself is deleted) — never hardcoded here or committed to source
control. If a key was ever shared somewhere it shouldn't have been (chat, a
public repo, a ticket), rotate it in Google Cloud Console (IAM & Admin ->
Service Accounts -> Keys).
"""

import base64
import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from log_sheet_automation.integrations import ocr_layout, ocr_pages

VISION_IMAGES_URL = "https://vision.googleapis.com/v1/images:annotate"
VISION_FILES_URL = "https://vision.googleapis.com/v1/files:annotate"
VISION_FEATURES = [{"type": "DOCUMENT_TEXT_DETECTION"}]
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


# ---------------------------------------------------------------------------
# source file -> pages
# ---------------------------------------------------------------------------

def load_pages(doc):
	"""Reads the sheet's Source Document and returns one entry per page (see
	ocr_pages.extract_pages)."""
	if not doc.source_document:
		frappe.throw(_("Attach the scanned/photographed log sheet (Source Document) before running OCR."))

	file_name = frappe.db.get_value("File", {"file_url": doc.source_document}, "name")
	if not file_name:
		frappe.throw(_("The attached Source Document could not be found. Remove it and attach the scan again."))
	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode("latin-1", errors="ignore")

	try:
		return ocr_pages.extract_pages(content, file_doc.file_name)
	except ocr_pages.PageExtractionError as e:
		frappe.throw(_(str(e)))


# ---------------------------------------------------------------------------
# pages -> one parsed result per page
# ---------------------------------------------------------------------------

def extract(doc, settings, pages):
	"""Returns a list with one dict per page:

		{"page_no", "run_id", "sheet", "raw_text", "word_dump"}

	`sheet` is ocr_layout.parse_sheet()'s result (template, header, rows with
	per-row confidence and review notes, warnings, period). Raises
	frappe.ValidationError on a hard provider failure (bad key, Vision API
	disabled, network error)."""
	if settings.ocr_provider_mode != "Google Vision API":
		return [_mock_result()]

	access_token = _get_google_access_token(load_service_account(settings))
	timeout = cint(settings.ocr_timeout_seconds) or 60
	stamp = now_datetime().strftime("%Y%m%d%H%M%S%f")

	results = []
	for page in pages:
		annotation = _annotate(page, access_token, timeout)
		words, full_text = ocr_layout.vision_words(annotation)
		results.append({
			"page_no": page["page_no"],
			"run_id": f"OCR-GOOGLEVISION-{stamp}-P{page['page_no']}",
			"sheet": ocr_layout.parse_sheet(words),
			"raw_text": full_text,
			"word_dump": _word_dump(annotation, page),
		})
	return results


def _word_dump(annotation, page):
	"""Compact copy of what Vision returned for one page (each word's text,
	confidence and four corner points). Attached to the sheet as a private
	JSON file so a misread layout can be diagnosed and the layout logic
	tuned/re-tested without paying for another Vision call."""
	words = []
	for word in ocr_layout.raw_words(annotation):
		flat = [round(value, 1) for point in word["points"] for value in point]
		words.append([word["text"], word["conf"]] + flat)
	return {
		"page_no": page["page_no"],
		"width": page.get("width"),
		"height": page.get("height"),
		"format": ["text", "confidence", "x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3"],
		"words": words,
	}


def _mock_result():
	"""Offline stand-in in the same shape as a real page result. Not
	selectable from Settings; kept for demos/tests without credentials."""
	today = frappe.utils.nowdate()
	tomorrow = frappe.utils.add_days(today, 1)
	rows = [
		{
			"day_label": "Mon", "log_date": today, "from_time": "08:00:00", "to_time": "18:00:00",
			"total_hours": 10.0, "normal_shift_hours": 8.0, "overtime_hours": 2.0, "breakdown_hours": 0.0,
			"work_description": "Mock OCR demo row", "confidence": 0.98, "notes": [],
		},
		{
			"day_label": "Tue", "log_date": str(tomorrow), "from_time": "08:00:00", "to_time": "17:30:00",
			"total_hours": 9.5, "normal_shift_hours": 8.0, "overtime_hours": 1.5, "breakdown_hours": 1.0,
			"work_description": "Mock OCR demo row", "confidence": 0.6, "notes": ["mock low-confidence row"],
		},
	]
	sheet = {
		"template": None, "header": {}, "rows": rows, "warnings": [],
		"period_start": today, "period_end": str(tomorrow), "month": None,
	}
	return {
		"page_no": 1,
		"run_id": f"OCR-MOCK-{now_datetime().strftime('%Y%m%d%H%M%S%f')}",
		"sheet": sheet,
		"raw_text": "",
		"word_dump": None,
	}


# ---------------------------------------------------------------------------
# Google Vision calls
# ---------------------------------------------------------------------------

def _annotate(page, access_token, timeout):
	"""One Vision call for one page. Returns that page's annotation dict
	(the object holding `fullTextAnnotation`)."""
	import requests

	encoded = base64.b64encode(page["content"]).decode("ascii")
	if page["kind"] == "pdf":
		url = VISION_FILES_URL
		payload = {
			"requests": [
				{
					"inputConfig": {"content": encoded, "mimeType": "application/pdf"},
					"features": VISION_FEATURES,
					"pages": [1],
				}
			]
		}
	else:
		url = VISION_IMAGES_URL
		payload = {"requests": [{"image": {"content": encoded}, "features": VISION_FEATURES}]}

	headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
	try:
		response = requests.post(url, headers=headers, json=payload, timeout=timeout)
	except requests.RequestException as e:
		frappe.throw(_("Could not reach Google Vision API (page {0}): {1}").format(page["page_no"], str(e)[:300]))

	if response.status_code != 200:
		frappe.throw(_explain_google_error(response, page["page_no"]))

	body = response.json()
	annotation = (body.get("responses") or [{}])[0]
	if page["kind"] == "pdf":
		if annotation.get("error"):
			frappe.throw(_("Google Vision API returned an error for page {0}: {1}").format(page["page_no"], annotation["error"].get("message")))
		annotation = (annotation.get("responses") or [{}])[0]
	if annotation.get("error"):
		frappe.throw(_("Google Vision API returned an error for page {0}: {1}").format(page["page_no"], annotation["error"].get("message")))
	return annotation


def _explain_google_error(response, page_no):
	"""Google's own error text plus, for the usual setup mistakes, what to do
	about it in plain words."""
	try:
		error = (response.json() or {}).get("error") or {}
	except ValueError:
		error = {}
	message = error.get("message") or response.text[:500]
	text = json.dumps(error) if error else message

	hint = ""
	if "SERVICE_DISABLED" in text or "has not been used in project" in text or "is disabled" in text:
		hint = _("Enable the 'Cloud Vision API' for this Google Cloud project (APIs & Services -> Library), wait a few minutes and try again.")
	elif "BILLING" in text.upper():
		hint = _("Billing is not enabled on this Google Cloud project. Vision API needs a billing account linked to the project.")
	elif response.status_code in (401, 403):
		hint = _("The service account in Settings is not allowed to use Vision API on its project. Check that the key belongs to the right project and has not been deleted or disabled.")
	elif response.status_code == 413 or "exceeds" in text.lower():
		hint = _("The page image is too large for Vision API.")
	elif response.status_code == 429:
		hint = _("Google's usage quota for Vision API was exceeded. Try again later or raise the quota.")

	return _("Google Vision API rejected page {0} ({1}): {2} {3}").format(page_no, response.status_code, message, hint)


# ---------------------------------------------------------------------------
# service account key
# ---------------------------------------------------------------------------

def parse_service_account_key(key_text):
	"""Checks that `key_text` really is a Google service account key file and
	returns it as a dict. Raises ValueError with a plain-language reason.
	Called when the key file is uploaded in Settings and before every OCR
	run (so a bad value stored by an older version is caught with a clear
	message instead of a cryptic failure)."""
	text = (key_text or "").strip()
	if not text:
		raise ValueError(_("Google Service Account Key (JSON) is empty."))
	try:
		info = json.loads(text)
	except ValueError:
		raise ValueError(
			_(
				"The Google Service Account Key is not valid JSON ({0} characters; a real key file is about 2,300). "
				"In Log Sheet Automation Settings, upload the .json key file exactly as downloaded from Google Cloud "
				"under Google Service Account Key File and save."
			).format(len(text))
		) from None

	if not isinstance(info, dict) or info.get("type") != "service_account":
		raise ValueError(
			_("This JSON is not a service account key (its \"type\" must be \"service_account\"). Create a key under Google Cloud Console -> IAM & Admin -> Service Accounts -> Keys -> Add key -> JSON.")
		)
	missing = [field for field in ("private_key", "client_email", "token_uri") if not info.get(field)]
	if missing:
		raise ValueError(_("The service account key JSON is missing: {0}.").format(", ".join(missing)))
	return info


def load_service_account(settings):
	key_text = settings.get_password("google_service_account_key", raise_exception=False)
	if not key_text:
		frappe.throw(_("No Google key is loaded. Upload the .json key file in Log Sheet Automation Settings and save."))
	try:
		return parse_service_account_key(key_text)
	except ValueError as e:
		frappe.throw(_("Log Sheet Automation Settings: {0}").format(e))


def _get_google_access_token(service_account_info):
	"""Exchanges the service account key for a short-lived OAuth2 access
	token, using the `google-auth` package (declared in pyproject.toml)."""
	try:
		from google.auth.transport.requests import Request as GoogleAuthRequest
		from google.oauth2 import service_account as google_service_account
	except ImportError:
		frappe.throw(
			_(
				"The 'google-auth' Python package is required for Google Vision OCR. Install it in "
				"this site's bench environment, e.g.: ./env/bin/pip install google-auth"
			)
		)

	try:
		credentials = google_service_account.Credentials.from_service_account_info(
			service_account_info, scopes=GOOGLE_SCOPES
		)
		credentials.refresh(GoogleAuthRequest())
	except Exception as e:
		frappe.throw(
			_(
				"Google did not accept the service account key ({0}). The key may have been deleted or disabled in "
				"Google Cloud Console, or was pasted incompletely."
			).format(str(e)[:300])
		)
	return credentials.token


# ---------------------------------------------------------------------------
# "Test Google Vision Connection" (button on Settings)
# ---------------------------------------------------------------------------

def test_connection(settings):
	"""Checks the whole chain with one tiny request: key is valid JSON ->
	Google issues a token -> Vision API is enabled, billed and answers."""
	import io

	from PIL import Image, ImageDraw

	info = load_service_account(settings)
	access_token = _get_google_access_token(info)

	image = Image.new("RGB", (360, 90), "white")
	ImageDraw.Draw(image).text((20, 35), "LOG SHEET 2468", fill="black")
	buffer = io.BytesIO()
	image.save(buffer, format="PNG")

	page = {"page_no": 1, "kind": "image", "content": buffer.getvalue()}
	annotation = _annotate(page, access_token, cint(settings.ocr_timeout_seconds) or 60)
	_words, text = ocr_layout.vision_words(annotation)
	return {
		"ok": True,
		"project_id": info.get("project_id"),
		"client_email": info.get("client_email"),
		"recognised_text": (text or "").strip(),
	}
