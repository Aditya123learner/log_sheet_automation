# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""OCR provider abstraction.

`Log Sheet Automation Settings.ocr_provider_mode` is locked to **Google
Vision API** — it's the only choice offered in Settings, and every OCR run
goes through it.

- **Mock** — returns a couple of fixed canned Daily Log rows, exactly in
  the shape a real run would. No longer reachable from Settings; requires
  no credentials, kept only as an offline smoke-test / reference.
- **Google Vision API** — calls Cloud Vision's `images:annotate` endpoint
  with `DOCUMENT_TEXT_DETECTION`, authenticating as the service account
  whose key JSON is pasted into `google_service_account_key` in Settings
  (that's the *only* field this provider needs — Vision has no per-project
  processor to create/train, unlike Document AI).

  CHANGE OF SHAPE (2026-10): this DocType moved from "one record per single
  day" to "one record per Weekly/Monthly sheet with a Daily Log child-table
  row per day" after the real paper log sheets were reviewed. OCR's job
  changed to match: instead of pulling four scalar hour values, `extract()`
  now returns a list of ROW dicts — one per day the scan shows — which
  `api.run_log_sheet_ocr` uses to populate the sheet's `daily_rows` child
  table (the header's working/breakdown/overtime-hour totals are then
  computed from those rows automatically — see equipment_log_sheet.py).

  The previous Azure Document Intelligence adapter has been removed rather
  than carried forward unreachable: Azure's custom extraction model was
  trained (conceptually) against the old four-scalar-field shape, and
  retraining it against a day-row table is a different problem, not a
  reachability flag — nothing was lost by deleting it, since it was never
  selectable from Settings anyway. If Azure (or Document AI) support is
  wanted again later, it would need to be rebuilt against this row shape.

  IMPORTANT DIFFERENCE FROM DOCUMENT AI: Vision API only does raw text
  recognition — it hands back the words/lines it read off the image, not
  labeled fields or table cells. This module has to reconstruct the Daily
  Log rows itself, line by line, from that raw text. Two things make that
  tunable WITHOUT another code deploy once Settings has been checked
  against a real scan:

  1. `Log Sheet Automation Settings.ocr_daily_row_pattern` — a single JSON
     string holding ONE regular expression with NAMED groups (date,
     from_time, to_time, total_hours, normal_shift_hours, overtime_hours,
     breakdown_hours, day_label, work_description — all optional except
     the ones you need) that is tried against every line of the OCR'd
     text. A line that matches becomes one Daily Log row; a line that
     doesn't match is skipped. Leave this setting blank to use
     DEFAULT_DAILY_ROW_PATTERN below. This is the "parameters we decide
     later" hook — once a real log sheet's OCR output has been seen, this
     is what gets tuned, not the code.
  2. `Log Sheet Automation Settings.ocr_field_label_patterns` — unchanged
     from the earlier scalar-field version; kept here as a secondary tool
     for `_match_field_in_text` which `_parse_daily_rows` falls back on if
     a whole-sheet total (rather than a per-day value) is ever needed.
     Most tuning work will happen on (1), not this.

  Every OCR run's audit trail entry ("OCR Completed" event on the log
  sheet) includes the raw text Vision actually recognized off the image,
  truncated to ~1000 characters — see `run_log_sheet_ocr` in api.py. Read
  that first to see what the row pattern actually needs to match.

  CAVEAT: Cloud Vision's plain text detection has no real notion of table
  structure — on a scanned/handwritten tabular form like these log sheets,
  its reading order can jump between columns rather than reading cleanly
  left-to-right along one row. DEFAULT_DAILY_ROW_PATTERN below is a
  reasonable starting guess at the printed column order on both the
  Weekly and Monthly templates, but it is UNVERIFIED against a live Vision
  response (no Google credentials were available in the environment this
  was built in) — treat every extracted row as needing a human check in AI
  Review until it's been tuned against real output. If line-based regex
  matching proves too fragile once real output is seen, a future version
  could switch to Vision's per-word bounding boxes (`textAnnotations`) and
  reconstruct rows by Y-coordinate instead — that's a bigger change, not
  attempted here.

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

All providers return the same shape from `extract()`, so
`api.run_log_sheet_ocr` and the rest of the workflow never need to know
which one ran.
"""

import re

import frappe
from frappe.utils import flt, now_datetime

# Best-effort label variants, tried in order, for a handful of whole-sheet
# totals that are occasionally printed as a single labelled value rather
# than inside the day-by-day table (kept mainly so _match_field_in_text has
# something to exercise; the Daily Log rows are what actually drive the
# workflow — see DEFAULT_DAILY_ROW_PATTERN below for those).
DEFAULT_FIELD_LABEL_PATTERNS = {
	"working_hours": [r"working\s*hours?", r"work\s*hrs?\.?", r"w\.?\s*hrs?\.?"],
	"breakdown_hours": [r"break\s*-?\s*down\s*hours?", r"breakdown\s*hrs?\.?", r"b\.?d\.?\s*hrs?\.?"],
	"overtime_hours": [r"over\s*-?\s*time\s*hours?", r"o\.?t\.?\s*hrs?\.?"],
}

# Tried, per line of the raw OCR text, to pull out one Daily Log row. Named
# groups map straight onto Equipment Log Sheet Day fields; a group that
# doesn't match for a given line is simply left blank/0 for that row.
# UNVERIFIED against a real Vision response — see the module docstring.
# Matches, loosely, the printed column order on both real templates:
#   Monthly:  Date | Working Time From-To | Total Hours | Normal Shift | OT Hours | ...
#   Weekly:   Day  | Date | Working Time From-To | Total Hours | Normal Shift Hours |
#             Over-Time Hours | Break Down Hours | ...
DEFAULT_DAILY_ROW_PATTERN = (
	r"(?:(?P<day_label>mon|tue|wed|thu|fri|sat|sun)\w*\s+)?"
	r"(?P<date>\d{1,2}[-/.][A-Za-z]{3,9}[-/.]?\d{2,4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})"
	r".*?(?P<from_time>\d{1,2}[:.]\d{2}\s*(?:am|pm)?)"
	r"\s*(?:to|-|–)\s*(?P<to_time>\d{1,2}[:.]\d{2}\s*(?:am|pm)?)"
	r".*?(?P<total_hours>\d{1,2}(?:\.\d+)?)"
	r"\s+(?P<normal_shift_hours>\d{1,2}(?:\.\d+)?)"
	r"\s+(?P<overtime_hours>\d{1,2}(?:\.\d+)?)"
	r"(?:\s+(?P<breakdown_hours>\d{1,2}(?:\.\d+)?))?"
)


def extract(doc, settings):
	"""Returns (run_id, rows, meta) — rows is a list of Daily Log row dicts
	(each with log_date/day_label/from_time/to_time/total_hours/
	normal_shift_hours/overtime_hours/breakdown_hours/work_description/
	confidence — see api.run_log_sheet_ocr for exactly how each key is
	used); meta is a dict of extra, provider-specific info for the audit
	trail ({"raw_text": <what Vision recognized>} for Google Vision, empty
	for Mock). Raises frappe.ValidationError on a hard provider failure
	(Mock never fails; Google Vision can)."""
	if settings.ocr_provider_mode == "Google Vision API":
		return _extract_google_vision(doc, settings)
	return _extract_mock()


def _extract_mock():
	run_id = f"OCR-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	rows = [
		{
			"day_label": "Mon",
			"log_date": frappe.utils.nowdate(),
			"from_time": "08:00:00",
			"to_time": "18:00:00",
			"total_hours": 8.0,
			"normal_shift_hours": 8.0,
			"overtime_hours": 1.5,
			"breakdown_hours": 0.0,
			"work_description": "Mock OCR demo row",
			"confidence": 0.98,
		},
		{
			"day_label": "Tue",
			"log_date": frappe.utils.add_days(frappe.utils.nowdate(), 1),
			"from_time": "08:00:00",
			"to_time": "17:30:00",
			"total_hours": 7.5,
			"normal_shift_hours": 7.5,
			"overtime_hours": 0.0,
			"breakdown_hours": 1.0,
			"work_description": "Mock OCR demo row",
			"confidence": 0.95,
		},
	]
	return run_id, rows, {}


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

	row_pattern = _get_daily_row_pattern(settings)
	rows = _parse_daily_rows(full_text, row_pattern)

	run_id = f"OCR-GOOGLEVISION-{now_datetime().strftime('%Y%m%d%H%M%S%f')}"

	# Truncated so a very noisy scan doesn't blow out the audit trail —
	# still long enough to see the row layout that actually needs matching.
	# Full text is in the API response itself if you need more.
	meta = {"raw_text": full_text[:1000]}
	return run_id, rows, meta


def _get_daily_row_pattern(settings):
	"""Settings.ocr_daily_row_pattern (a plain string, not JSON — just one
	regex) overrides DEFAULT_DAILY_ROW_PATTERN above. Blank/unset falls back
	to the default. See the module docstring for the named groups it should
	capture."""
	raw = (settings.get("ocr_daily_row_pattern") or "").strip()
	if not raw:
		return DEFAULT_DAILY_ROW_PATTERN
	try:
		re.compile(raw)
	except re.error as e:
		frappe.throw(
			frappe._("Log Sheet Automation Settings > OCR Daily Row Pattern is not a valid regular expression: {0}").format(e)
		)
	return raw


def _parse_daily_rows(full_text, row_pattern):
	"""Tries row_pattern against every non-blank line of full_text. A line
	that matches becomes one Daily Log row. Confidence here is NOT something
	Google gave us (Vision doesn't score rows the way Document AI scores
	fields); a row counts as low-confidence (0.0, flagged for review) if it
	has no total_hours capture at all, otherwise 0.6 — a flat, cautious
	guess, since this whole extraction strategy is unverified against a
	real scan (see module docstring)."""
	rows = []
	for line in full_text.splitlines():
		line = line.strip()
		if not line:
			continue
		match = re.search(row_pattern, line, re.IGNORECASE)
		if not match:
			continue
		groups = match.groupdict()
		total_hours = _to_float(groups.get("total_hours"))
		rows.append(
			{
				"day_label": (groups.get("day_label") or "").title() or None,
				"log_date": groups.get("date"),
				"from_time": groups.get("from_time"),
				"to_time": groups.get("to_time"),
				"total_hours": total_hours,
				"normal_shift_hours": _to_float(groups.get("normal_shift_hours")),
				"overtime_hours": _to_float(groups.get("overtime_hours")),
				"breakdown_hours": _to_float(groups.get("breakdown_hours")),
				"work_description": groups.get("work_description"),
				"confidence": 0.6 if total_hours else 0.0,
			}
		)
	return rows


def _to_float(value):
	if value is None or value == "":
		return 0.0
	try:
		return flt(value)
	except (TypeError, ValueError):
		return 0.0


def _get_field_label_patterns(settings):
	"""Merges Settings.ocr_field_label_patterns (a JSON object of
	{field_name: [pattern, ...]}, edited from the Desk UI — no code deploy
	needed) over DEFAULT_FIELD_LABEL_PATTERNS. A field the JSON doesn't
	mention, or an empty/blank setting, falls back to the default for that
	field. Malformed JSON is a hard error (not silently ignored) so a typo
	doesn't quietly revert to defaults without anyone noticing. Used by
	_match_field_in_text for whole-sheet totals, not the per-day rows."""
	import json as json_module

	raw = (settings.get("ocr_field_label_patterns") or "").strip()
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
	NOT something Google gave us; it's just 0.9 if a label+number match was
	found, 0.0 if it wasn't. Used for optional whole-sheet totals, not the
	per-day Daily Log rows (see _parse_daily_rows)."""
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
