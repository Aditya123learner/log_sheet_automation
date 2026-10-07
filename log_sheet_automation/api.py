# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Whitelisted API surface for Log Sheet Automation.

Nine workflow endpoints, matching the SRS's Appendix B "controller methods"
(plus `test_ocr_connection`, a Settings helper). Two are
guest-accessible (the public client approval page hits these with no
session): `get_log_sheet_approval_snapshot` (GET) and
`record_log_sheet_client_decision` (POST). Everything else requires one of
the module's own roles, checked explicitly below with `frappe.get_roles()`
rather than relying on DocType-level permissions alone, because these are
workflow *actions* (approve/reject/close), not plain field writes — see the
Technical Design document §3.1 for why the two are checked separately.

Every mutating endpoint here is POST-only in practice: call it with GET and
nothing gets persisted (this was confirmed empirically against Frappe
during the original prototype — a GET request against a whitelisted method
does not reliably auto-commit the write). `get_log_sheet_approval_snapshot`
is the one read-only, GET-safe exception.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, generate_hash, get_datetime, now_datetime, sha256_hash

from log_sheet_automation import notifications
from log_sheet_automation.integrations import ocr as ocr_integration
from log_sheet_automation.integrations import sap as sap_integration

GENERIC_GUEST_ERROR = {"ok": False, "error": "This approval link is invalid or has expired."}


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _require_role(*allowed_roles):
	user_roles = set(frappe.get_roles(frappe.session.user))
	if not (user_roles & set(allowed_roles)):
		frappe.throw(_("You are not permitted to perform this action."), frappe.PermissionError)


def _get_settings():
	return frappe.get_cached_doc("Log Sheet Automation Settings")


def _log_event(doc, event_type, prior_state, new_state, decision, actor_role, comment=None, reason_code=None,
                snapshot_hash=None, actor=None):
	doc.append(
		"approval_events",
		{
			"event_type": event_type,
			"prior_state": prior_state,
			"new_state": new_state,
			"decision": decision,
			"actor": actor or frappe.session.user,
			"actor_role": actor_role,
			"event_time": now_datetime(),
			"reason_code": reason_code,
			"comment": comment,
			"snapshot_hash": snapshot_hash,
		},
	)


# ---------------------------------------------------------------------------
# 1. OCR (Google Vision API)
# ---------------------------------------------------------------------------

# A scan with more pages than this is read in a background job instead of
# inside the browser's request (each page is one Vision call of a few
# seconds; a web request is cut off after about two minutes).
OCR_SYNC_PAGE_LIMIT = 6

# ocr_layout header key -> Equipment Log Sheet field holding the value
# exactly as it was read off the scan (for the operator to compare).
OCR_HEADER_FIELDS = {
	"operator": "ocr_operator_name",
	"crane_model": "ocr_crane_model",
	"regn_no": "ocr_regn_no",
	"site": "ocr_site_text",
	"client": "ocr_client_text",
}


@frappe.whitelist()
def run_log_sheet_ocr(name):
	"""Read the attached scan with Google Vision and fill in the Daily Log.

	One physical log sheet = one Equipment Log Sheet. The real scans arrive
	as multi-page PDFs with one sheet per page, so: page 1 is written onto
	the sheet OCR was run from, and every further page gets its own new
	Equipment Log Sheet (same Operating Site, linked back through
	`source_parent_sheet` / `source_page_no`). Running OCR again updates
	those same sheets rather than creating duplicates.

	Nothing OCR reads is trusted blindly: every row carries a confidence and
	anything doubtful is listed under Validation Results and keeps the sheet
	in AI Review until the operator ticks OCR Review Complete (BR-006)."""
	_require_role("Log Sheet Operator", "Log Sheet Manager", "System Manager")

	doc = frappe.get_doc("Equipment Log Sheet", name)
	if doc.workflow_state not in ("Draft", "AI Review"):
		frappe.throw(_("OCR can only be run while the log is in Draft or AI Review."))

	settings = _get_settings()
	pages = _ocr_pages_for(doc)

	if len(pages) > OCR_SYNC_PAGE_LIMIT:
		frappe.db.set_value("Equipment Log Sheet", doc.name, "ocr_status", "Queued", update_modified=False)
		frappe.enqueue(
			"log_sheet_automation.api.run_log_sheet_ocr_job",
			queue="long",
			timeout=1800,
			enqueue_after_commit=True,
			sheet_name=doc.name,
		)
		return {"ok": True, "queued": True, "pages": len(pages)}

	return _run_ocr(doc, settings, pages)


def run_log_sheet_ocr_job(sheet_name):
	"""Background-job twin of run_log_sheet_ocr for long scans. Tells the
	operator's open form when it finishes (or why it failed)."""
	name = sheet_name
	user = frappe.session.user
	try:
		doc = frappe.get_doc("Equipment Log Sheet", name)
		result = _run_ocr(doc, _get_settings(), _ocr_pages_for(doc))
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.db.set_value("Equipment Log Sheet", name, "ocr_status", "Failed", update_modified=False)
		frappe.log_error(title=f"Log sheet OCR failed for {name}")
		frappe.db.commit()
		result = {"ok": False, "error": str(e)}
	frappe.publish_realtime("log_sheet_ocr_done", dict(result, name=name), user=user)


def _ocr_pages_for(doc):
	pages = ocr_integration.load_pages(doc)
	if doc.source_parent_sheet and cint(doc.source_page_no):
		# This sheet was itself created from one page of a multi-page scan:
		# re-reading it must only touch its own page.
		own = [page for page in pages if page["page_no"] == cint(doc.source_page_no)]
		if not own:
			frappe.throw(_("Page {0} no longer exists in the attached Source Document.").format(doc.source_page_no))
		return own
	return pages


def _run_ocr(doc, settings, pages):
	threshold = flt(settings.mock_ocr_confidence_low_threshold) or 0.75
	results = ocr_integration.extract(doc, settings, pages)
	keep_dump = cint(settings.get("ocr_keep_word_dump"))
	is_page_sheet = bool(doc.source_parent_sheet)

	summary = []
	for index, result in enumerate(results):
		if index == 0:
			if len(results) > 1 and not is_page_sheet:
				doc.source_page_no = result["page_no"]
			outcome = _apply_ocr_result(doc, result, settings, threshold)
			doc.flags.ignore_mandatory = True
			doc.save()
			target = doc
		else:
			target, skip_reason = _sheet_for_page(doc, result["page_no"])
			if not target:
				summary.append({"page_no": result["page_no"], "skipped": skip_reason})
				continue
			# One bad page (e.g. it duplicates an existing sheet, BR-004) must
			# not throw away the pages that were read fine.
			frappe.db.savepoint("log_sheet_ocr_page")
			try:
				outcome = _apply_ocr_result(target, result, settings, threshold, match_equipment=True)
				target.flags.ignore_mandatory = True
				if target.is_new():
					target.insert()
				else:
					target.save()
			except frappe.ValidationError as e:
				frappe.db.rollback(save_point="log_sheet_ocr_page")
				frappe.clear_messages()
				summary.append({"page_no": result["page_no"], "skipped": str(e)})
				continue

		_blank_unread_times(target, result)
		if keep_dump and result.get("word_dump"):
			_attach_word_dump(target, result)
		summary.append(dict(outcome, name=target.name, page_no=result["page_no"]))

	first = summary[0]
	return {
		"ok": True,
		"run_id": results[0]["run_id"],
		"rows_extracted": first.get("rows", 0),
		"low_confidence": first.get("low_confidence", True),
		"workflow_state": doc.workflow_state,
		"sheets": summary,
	}


def _sheet_for_page(parent, page_no):
	"""The Equipment Log Sheet holding page `page_no` of `parent`'s scan:
	the one created by an earlier run if there is one, else a new one."""
	existing = frappe.db.get_value(
		"Equipment Log Sheet",
		{"source_parent_sheet": parent.name, "source_page_no": page_no, "workflow_state": ["!=", "Void"]},
		"name",
	)
	if existing:
		sheet = frappe.get_doc("Equipment Log Sheet", existing)
		if sheet.workflow_state not in ("Draft", "AI Review"):
			return None, _("{0} already exists for this page and is {1}; it was left unchanged.").format(
				sheet.name, sheet.workflow_state
			)
		return sheet, None

	sheet = frappe.new_doc("Equipment Log Sheet")
	sheet.update(
		{
			"operating_site": parent.operating_site,
			"customer": parent.customer,
			"operator_user": parent.operator_user,
			"client_recipient": parent.client_recipient,
			"ocr_template": parent.ocr_template,
			"source_document": parent.source_document,
			"source_parent_sheet": parent.name,
			"source_page_no": page_no,
		}
	)
	return sheet, None


def _match_equipment(header):
	"""Best-effort: the Asset whose name contains the Regn. No. / Crane Model
	read off the sheet. Only an unambiguous single match is used; otherwise
	Equipment is left blank and flagged for the operator."""
	for value in (header.get("regn_no"), header.get("crane_model")):
		if not value or len(value) < 4:
			continue
		matches = frappe.get_all(
			"Asset",
			filters={"docstatus": ["<", 2]},
			or_filters=[["asset_name", "like", f"%{value}%"], ["name", "=", value]],
			pluck="name",
			limit=2,
		)
		if len(matches) == 1:
			return matches[0]
	return None


def _apply_ocr_result(doc, result, settings, threshold, match_equipment=False):
	"""Writes one page's OCR result onto `doc` (not saved here)."""
	sheet = result["sheet"]
	header = sheet.get("header") or {}
	rows = sheet.get("rows") or []
	run_id = result["run_id"]
	prior_state = doc.workflow_state or "Draft"

	if sheet.get("template"):
		doc.sheet_template = sheet["template"]

	# OCR populates the sheet fresh from the scan — re-running it replaces
	# the previous run's rows rather than appending alongside them.
	doc.set("daily_rows", [])
	for row in rows:
		doc.append(
			"daily_rows",
			{
				"day_label": row.get("day_label"),
				"log_date": row.get("log_date"),
				"from_time": row.get("from_time"),
				"to_time": row.get("to_time"),
				"total_hours": row.get("total_hours") or 0,
				"normal_shift_hours": row.get("normal_shift_hours") or 0,
				"overtime_hours": row.get("overtime_hours") or 0,
				"breakdown_hours": row.get("breakdown_hours") or 0,
				"work_description": row.get("work_description"),
			},
		)

	if sheet.get("period_start"):
		doc.period_start_date = sheet["period_start"]
		doc.period_end_date = sheet["period_end"]
		doc.month = sheet.get("month") or doc.month
	elif header.get("month_text") and not doc.month:
		doc.month = header["month_text"]

	if header.get("log_sheet_no"):
		doc.log_sheet_no = header["log_sheet_no"]
	if doc.sheet_template == "Weekly":
		for fieldname in ("hour_meter_opening", "hour_meter_closing"):
			if header.get(fieldname):
				doc.set(fieldname, header[fieldname])
	for key, fieldname in OCR_HEADER_FIELDS.items():
		doc.set(fieldname, (header.get(key) or "")[:140] or None)

	if match_equipment and not doc.equipment:
		doc.equipment = _match_equipment(header)

	# Only the latest OCR run's checks are "current".
	for check in doc.validation_table:
		if check.validation_type == "OCR":
			check.is_current = 0

	def add_check(code, status, message, summary=None):
		doc.append(
			"validation_table",
			{
				"validation_type": "OCR",
				"run_id": run_id,
				"check_code": code[:140],
				"status": status,
				"message": message,
				"input_summary": json.dumps(summary or {}),
				"source_timestamp": now_datetime(),
				"provider_mode": settings.ocr_provider_mode,
				"is_current": 1,
			},
		)

	low_confidence = False
	for warning in sheet.get("warnings") or []:
		add_check("page", "Exception", warning)
		low_confidence = True
	if not rows:
		add_check("no_rows", "Exception", "OCR did not find any Daily Log rows on this page. Enter them by hand, or attach a clearer scan and run OCR again.")
		low_confidence = True
	if not doc.equipment:
		add_check(
			"equipment", "Exception",
			f"Equipment was not set: no Asset matches Regn. No. '{header.get('regn_no') or '?'}' / "
			f"Crane Model '{header.get('crane_model') or '?'}' as read from the sheet. Select it by hand.",
		)
		low_confidence = True

	for i, row in enumerate(rows, start=1):
		confidence = flt(row.get("confidence"))
		status = "Passed" if confidence >= threshold else "Exception"
		low_confidence = low_confidence or status == "Exception"
		message = f"Row {i} ({row.get('log_date') or 'date not read'}): total {flt(row.get('total_hours')):g} h, confidence {confidence:.2f}"
		if row.get("notes"):
			message += " — " + "; ".join(row["notes"])
		add_check(
			f"day_{i}_{row.get('log_date') or 'unknown'}", status, message,
			{"log_date": row.get("log_date"), "confidence": confidence},
		)

	doc.ocr_status = "Completed"
	doc.ocr_provider_result_id = run_id
	doc.ocr_review_complete = 0 if low_confidence else 1
	doc.workflow_state = "AI Review"

	comment = (
		f"{settings.ocr_provider_mode} OCR run {run_id} (page {result['page_no']} of the scan): "
		f"read {len(rows)} Daily Log row(s) as a {sheet.get('template') or 'not recognised'} sheet. "
		f"Needs review: {low_confidence}"
	)
	if result.get("raw_text"):
		# The text exactly as Vision recognised it, so whoever reviews this
		# run can compare it with what was filled in.
		comment += f"\n\nRaw OCR text:\n{result['raw_text'][:1500]}"

	_log_event(doc, "System", prior_state, "AI Review", "OCR Completed", "Log Sheet Operator", comment=comment)
	return {"rows": len(rows), "low_confidence": low_confidence, "template": sheet.get("template")}


def _blank_unread_times(doc, result):
	"""Frappe fills every empty Time field of a NEW child row with the current
	clock time when the parent is saved (create_new.set_dynamic_default_values).
	For a From/To that OCR could not read that would silently store "now" —
	e.g. 12:24:05 — as if it had been read off the sheet. Put the blank back."""
	rows = (result.get("sheet") or {}).get("rows") or []
	for child, row in zip(doc.daily_rows, rows):
		for fieldname in ("from_time", "to_time"):
			if not row.get(fieldname) and child.get(fieldname):
				child.set(fieldname, None)
				if child.get("name"):
					frappe.db.set_value("Equipment Log Sheet Day", child.name, fieldname, None, update_modified=False)


def _attach_word_dump(doc, result):
	"""Saves Vision's word positions for this page as a private JSON file on
	the sheet (replacing the previous run's). Purely diagnostic: failing to
	save it must never fail the OCR run."""
	try:
		prefix = f"{doc.name}-ocr-words"
		for old in frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Equipment Log Sheet",
				"attached_to_name": doc.name,
				"file_name": ["like", f"{prefix}%"],
			},
			pluck="name",
		):
			frappe.delete_doc("File", old, ignore_permissions=True, force=True)
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{prefix}-page-{result['page_no']}.json",
				"attached_to_doctype": "Equipment Log Sheet",
				"attached_to_name": doc.name,
				"is_private": 1,
				"content": json.dumps(result["word_dump"]),
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title=f"Could not save OCR word dump for {doc.name}")


@frappe.whitelist()
def test_ocr_connection():
	"""Settings button: prove the Google key and the Vision API work, with
	one tiny request, before anyone runs OCR on a real sheet."""
	_require_role("Log Sheet Manager", "System Manager")
	return ocr_integration.test_connection(_get_settings())


# ---------------------------------------------------------------------------
# 2. Generate the client approval link
# ---------------------------------------------------------------------------

@frappe.whitelist()
def generate_log_sheet_client_link(name):
	_require_role("Log Sheet Operator", "Log Sheet Manager", "System Manager")

	doc = frappe.get_doc("Equipment Log Sheet", name)

	if doc.workflow_state in ("Closed", "Void", "Client Approval Pending"):
		frappe.throw(_("Client approval link cannot be generated while the log is {0}.").format(doc.workflow_state))
	if doc.client_approval_status == "Approved":
		frappe.throw(_("This log already has an approved client decision."))

	# BR-001: mandatory identity fields
	missing = [
		fn for fn in ("operating_site", "equipment", "sheet_template", "month", "period_start_date", "period_end_date")
		if not doc.get(fn)
	]
	if not doc.operator_user:
		missing.append("operator_user")
	if missing:
		frappe.throw(_("Cannot generate client link, missing mandatory fields: {0} (BR-001).").format(", ".join(missing)))

	# BR-006: low-confidence OCR fields must be confirmed first
	if doc.ocr_status == "Completed" and not doc.ocr_review_complete:
		frappe.throw(_("Confirm low-confidence OCR fields before generating the client link (BR-006)."))

	# BR-005: an active commercial mapping must exist for this site/equipment/period
	mapping = frappe.get_all(
		"Site Equipment Commercial Mapping",
		filters={
			"operating_site": doc.operating_site,
			"equipment": doc.equipment,
			"is_active": 1,
			"valid_from": ["<=", doc.period_start_date],
		},
		fields=["name", "sap_sales_order", "sap_sales_order_item", "work_order_reference", "uom", "rate_key", "billing_rule"],
		order_by="valid_from desc",
		limit_page_length=1,
	)
	if not mapping:
		frappe.throw(_("No active Site Equipment Commercial Mapping for this site/equipment/period (BR-005)."))

	m = mapping[0]
	doc.sap_sales_order = m.sap_sales_order
	doc.sap_sales_order_item = m.sap_sales_order_item
	doc.work_order_reference = m.work_order_reference
	doc.uom = m.uom
	doc.rate_key = m.rate_key
	if not doc.billing_rule:
		doc.billing_rule = m.billing_rule

	snapshot = {
		"name": doc.name,
		"sheet_template": doc.sheet_template,
		"month": doc.month,
		"period_start_date": str(doc.period_start_date) if doc.period_start_date else None,
		"period_end_date": str(doc.period_end_date) if doc.period_end_date else None,
		"operating_site": doc.operating_site,
		"equipment": doc.equipment,
		"working_hours": doc.working_hours,
		"idle_hours": doc.idle_hours,
		"standby_hours": doc.standby_hours,
		"breakdown_hours": doc.breakdown_hours,
		"overtime_hours": doc.overtime_hours,
		# Daily rows are part of the snapshot too, so a per-day edit (not
		# just a header-field edit) after the link is generated is also
		# caught by comparing client_snapshot_hash at decision time.
		"daily_rows": [
			{
				"log_date": str(row.log_date) if row.log_date else None,
				"total_hours": row.total_hours,
				"normal_shift_hours": row.normal_shift_hours,
				"overtime_hours": row.overtime_hours,
				"breakdown_hours": row.breakdown_hours,
			}
			for row in doc.daily_rows
		],
	}
	snapshot_hash = sha256_hash(json.dumps(snapshot, sort_keys=True))

	token = generate_hash(length=32)
	token_hash = sha256_hash(token)

	settings = _get_settings()
	ttl_hours = cint(settings.client_token_ttl_hours) or 48
	expiry = add_to_date(now_datetime(), hours=ttl_hours)

	doc.client_token_hash = token_hash
	doc.client_token_expiry = expiry
	doc.client_snapshot_hash = snapshot_hash
	doc.client_approval_status = "Pending"
	doc.client_decision_on = None
	doc.workflow_state = "Client Approval Pending"

	_log_event(
		doc, "System", "Ready for Client", "Client Approval Pending", "Link Generated", "Log Sheet Operator",
		comment=f"Client approval link generated, expires {expiry}.", snapshot_hash=snapshot_hash,
	)

	doc.save()

	approval_path = f"/log-sheet-approval?token={token}"
	delivery_status = notifications.send_client_approval_link(doc, approval_path, expiry)

	# Log delivery outcome on its own audit row (after the doc is already
	# saved above) rather than folding it into the "Link Generated" event —
	# a delivery failure here must never roll back or block link generation.
	doc.reload()
	_log_event(
		doc, "System", doc.workflow_state, doc.workflow_state, "Notification Sent",
		"System", comment=delivery_status,
	)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	return {
		"ok": True,
		"token": token,  # returned once, never persisted — see README security notes
		"approval_path": approval_path,
		"expiry": str(expiry),
		"workflow_state": doc.workflow_state,
		"notification": delivery_status,
	}


# ---------------------------------------------------------------------------
# 3. Guest: read the approval snapshot (GET, allow_guest)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_log_sheet_approval_snapshot(token=None):
	if not token:
		frappe.response["message"] = GENERIC_GUEST_ERROR
		return

	token_hash = sha256_hash(token)
	rows = frappe.get_all(
		"Equipment Log Sheet",
		filters={"client_token_hash": token_hash, "client_approval_status": "Pending"},
		fields=["name", "client_token_expiry"],
		limit_page_length=1,
	)
	if not rows:
		frappe.response["message"] = GENERIC_GUEST_ERROR
		return

	row = rows[0]
	if now_datetime() > get_datetime(row.client_token_expiry):
		frappe.response["message"] = GENERIC_GUEST_ERROR
		return

	doc = frappe.get_doc("Equipment Log Sheet", row.name)
	frappe.response["message"] = {
		"ok": True,
		"site": doc.operating_site,
		"equipment": doc.equipment,
		"sheet_template": doc.sheet_template,
		"month": doc.month,
		"period_start_date": str(doc.period_start_date) if doc.period_start_date else None,
		"period_end_date": str(doc.period_end_date) if doc.period_end_date else None,
		"working_hours": doc.working_hours,
		"idle_hours": doc.idle_hours,
		"standby_hours": doc.standby_hours,
		"breakdown_hours": doc.breakdown_hours,
		"overtime_hours": doc.overtime_hours,
		"daily_rows": [
			{
				"log_date": str(row.log_date) if row.log_date else None,
				"day_label": row.day_label,
				"from_time": str(row.from_time) if row.from_time else None,
				"to_time": str(row.to_time) if row.to_time else None,
				"total_hours": row.total_hours,
				"normal_shift_hours": row.normal_shift_hours,
				"overtime_hours": row.overtime_hours,
				"breakdown_hours": row.breakdown_hours,
				"work_description": row.work_description,
			}
			for row in doc.daily_rows
		],
		"snapshot_hash": doc.client_snapshot_hash,
	}


# ---------------------------------------------------------------------------
# 4. Guest: record the client's Approve/Reject decision (POST, allow_guest)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def record_log_sheet_client_decision(token=None, decision=None, comment=None):
	if not token or decision not in ("Approve", "Reject"):
		frappe.response["message"] = GENERIC_GUEST_ERROR
		return

	token_hash = sha256_hash(token)
	rows = frappe.get_all(
		"Equipment Log Sheet",
		filters={"client_token_hash": token_hash, "client_approval_status": "Pending"},
		fields=["name", "client_token_expiry"],
		limit_page_length=1,
	)
	if not rows:
		frappe.response["message"] = GENERIC_GUEST_ERROR
		return

	row = rows[0]
	if now_datetime() > get_datetime(row.client_token_expiry):
		frappe.response["message"] = GENERIC_GUEST_ERROR
		return
	if decision == "Reject" and not comment:
		frappe.response["message"] = {"ok": False, "error": "A comment is required to reject."}
		return

	doc = frappe.get_doc("Equipment Log Sheet", row.name)
	doc.client_decision_on = now_datetime()
	doc.client_comment = comment

	if decision == "Approve":
		doc.client_approval_status = "Approved"
		# Captured now so equipment_log_sheet.py's BR-007 check can detect
		# ANY later edit — including inside the daily_rows child table — by
		# comparing against this baseline on every subsequent save.
		doc.approved_content_hash = doc.compute_content_hash()
		# BR-003: approval opens BOTH parallel gates at once.
		doc.maintenance_status = "Pending"
		doc.sap_validation_status = "Pending"
		new_state = "Approved"
	else:
		doc.client_approval_status = "Rejected"
		doc.client_token_hash = None
		doc.client_token_expiry = None
		new_state = "Operator Rework"

	_log_event(
		doc, "Client Decision", "Client Approval Pending", new_state, decision,
		"Guest Client Approver", comment=comment, snapshot_hash=doc.client_snapshot_hash,
		actor="Guest Client Approver",
	)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.response["message"] = {"ok": True, "decision": decision, "log": doc.name}


# ---------------------------------------------------------------------------
# 5. Maintenance decision
# ---------------------------------------------------------------------------

@frappe.whitelist()
def record_log_sheet_maintenance_decision(name, decision, reason_code=None, comment=None):
	_require_role("Log Sheet Maintenance Approver", "Log Sheet Manager", "System Manager")

	if decision not in ("Approved", "Rejected"):
		frappe.throw(_("Decision must be Approved or Rejected."))

	doc = frappe.get_doc("Equipment Log Sheet", name)
	if doc.maintenance_status != "Pending":
		frappe.throw(_("Maintenance decision can only be recorded while Maintenance status is Pending."))

	# BR-008: reason code + comment mandatory on rejection
	if decision == "Rejected" and (not reason_code or not comment):
		frappe.throw(_("Reason code and comment are mandatory on rejection (BR-008)."))

	doc.maintenance_status = decision
	doc.maintenance_reason_code = reason_code
	doc.maintenance_comment = comment

	_log_event(
		doc, "Maintenance Decision", doc.workflow_state,
		"Operator Rework" if decision == "Rejected" else "Parallel Validation",
		decision, "Log Sheet Maintenance Approver", comment=comment, reason_code=reason_code,
	)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"ok": True, "workflow_state": doc.workflow_state, "maintenance_status": doc.maintenance_status}


# ---------------------------------------------------------------------------
# 6. SAP validation (Mock or Live, per Settings)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def run_log_sheet_sap_validation(name):
	"""Dispatches to the Mock or Live provider per
	`Log Sheet Automation Settings.sap_provider_mode` — see
	log_sheet_automation/integrations/sap.py."""
	_require_role("Log Sheet Operations Approver", "Log Sheet Sales Resolver", "Log Sheet Manager", "System Manager")

	doc = frappe.get_doc("Equipment Log Sheet", name)
	if doc.sap_validation_status not in ("Pending", "Exception"):
		frappe.throw(_("SAP validation can only be run while status is Pending or Exception."))

	settings = _get_settings()
	run_id, checks = sap_integration.validate(doc, settings)

	for row in doc.validation_table:
		if row.validation_type == "SAP":
			row.is_current = 0

	if any(c["status"] == "Failed" for c in checks):
		overall = "Failed"
	elif any(c["status"] == "Exception" for c in checks):
		overall = "Exception"
	else:
		overall = "Passed"

	for c in checks:
		doc.append(
			"validation_table",
			{
				"validation_type": "SAP",
				"run_id": run_id,
				"check_code": c["code"],
				"status": c["status"],
				"message": c["message"],
				"input_summary": json.dumps({"sap_sales_order": doc.sap_sales_order, "sap_sales_order_item": doc.sap_sales_order_item}),
				"source_timestamp": now_datetime(),
				"provider_mode": settings.sap_provider_mode,
				"is_current": 1,
			},
		)

	doc.sap_validation_status = overall
	doc.sap_current_run_id = run_id

	_log_event(
		doc, "SAP Validation", doc.workflow_state,
		"Sales Action Pending" if overall in ("Exception", "Failed") else "Parallel Validation",
		overall, "System", comment=f"{settings.sap_provider_mode} SAP validation run {run_id}: overall {overall}.",
	)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"ok": True, "overall": overall, "run_id": run_id, "workflow_state": doc.workflow_state}


# ---------------------------------------------------------------------------
# 7. Sales: request SAP revalidation after fixing the underlying issue
# ---------------------------------------------------------------------------

@frappe.whitelist()
def request_log_sheet_sap_revalidation(name, comment):
	_require_role("Log Sheet Sales Resolver", "Log Sheet Manager", "System Manager")

	if not comment:
		frappe.throw(_("A corrective-action comment is required to request revalidation."))

	doc = frappe.get_doc("Equipment Log Sheet", name)
	if doc.sap_validation_status != "Exception":
		frappe.throw(_("Revalidation can only be requested while SAP status is Exception."))

	_log_event(
		doc, "Sales Action", doc.workflow_state, doc.workflow_state, "Revalidation Requested",
		"Log Sheet Sales Resolver", comment=comment,
	)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"ok": True, "step": "logged corrective action, now call run_log_sheet_sap_validation to rerun"}


# ---------------------------------------------------------------------------
# 8. Operations decision (+ billable-hours calculation)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def record_log_sheet_operations_decision(name, decision, comment=None):
	_require_role("Log Sheet Operations Approver", "Log Sheet Manager", "System Manager")

	if decision not in ("Approved", "Returned"):
		frappe.throw(_("Decision must be Approved or Returned."))

	doc = frappe.get_doc("Equipment Log Sheet", name)

	if decision == "Returned" and not comment:
		frappe.throw(_("A comment is mandatory when returning a log."))

	if decision == "Approved":
		# BR-010: server-side gate enforcement regardless of UI state.
		if doc.client_approval_status != "Approved" or doc.maintenance_status != "Approved" or doc.sap_validation_status != "Passed":
			frappe.throw(_("Cannot approve: client, Maintenance and SAP gates must all be clear (BR-010)."))
		if not doc.billing_rule:
			frappe.throw(_("No Billing Rule set on this log; cannot calculate billable hours."))

		trace = _calculate_billable_hours(doc)
		doc.calculation_trace = json.dumps(trace, indent=2)
		doc.operations_status = "Approved"
		doc.billing_status = "Ready"
		new_state = "Billing Ready"
	else:
		doc.operations_status = "Returned"
		doc.operations_comment = comment
		new_state = "Operator Rework"

	_log_event(doc, "Operations Decision", doc.workflow_state, new_state, decision, "Log Sheet Operations Approver", comment=comment)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"ok": True, "workflow_state": doc.workflow_state, "billable_hours": doc.billable_hours}


def _calculate_billable_hours(doc):
	"""BR-011's arithmetic: gross hours (by rule toggle) -> minimum floor ->
	rounding. Returns a full trace dict so a billing dispute can be answered
	straight from the record (`calculation_trace`)."""
	rule = frappe.get_doc("Log Sheet Billing Rule", doc.billing_rule)

	gross_hours = flt(doc.working_hours)
	if rule.bill_idle_hours:
		gross_hours += flt(doc.idle_hours)
	if rule.bill_standby_hours:
		gross_hours += flt(doc.standby_hours)
	if rule.bill_breakdown_hours:
		gross_hours += flt(doc.breakdown_hours)

	minimum = flt(rule.minimum_daily_hours)
	after_minimum = gross_hours if gross_hours > minimum else minimum

	increment = flt(rule.rounding_increment) or 0.5
	units = after_minimum / increment
	whole_units = units // 1
	if rule.rounding_method == "Up":
		if units > whole_units:
			whole_units += 1
	elif rule.rounding_method == "Down":
		pass
	else:  # Nearest
		if (units - whole_units) >= 0.5:
			whole_units += 1
	billable = whole_units * increment

	doc.billable_hours = billable
	doc.billing_rule_version = rule.rule_version

	return {
		"working_hours": doc.working_hours,
		"idle_hours": doc.idle_hours,
		"standby_hours": doc.standby_hours,
		"breakdown_hours": doc.breakdown_hours,
		"bill_idle_hours": rule.bill_idle_hours,
		"bill_standby_hours": rule.bill_standby_hours,
		"bill_breakdown_hours": rule.bill_breakdown_hours,
		"gross_hours": gross_hours,
		"minimum_daily_hours": minimum,
		"after_minimum": after_minimum,
		"rounding_increment": increment,
		"rounding_method": rule.rounding_method,
		"billable_hours": billable,
	}


# ---------------------------------------------------------------------------
# 9. Billing outcome
# ---------------------------------------------------------------------------

@frappe.whitelist()
def record_log_sheet_billing_outcome(name, action, sap_document_reference=None, hold_reason=None,
                                      manager_override_reason=None):
	_require_role("Log Sheet Billing User", "Log Sheet Manager", "System Manager")

	if action not in ("Close", "Hold"):
		frappe.throw(_("Action must be Close or Hold."))

	doc = frappe.get_doc("Equipment Log Sheet", name)
	if doc.workflow_state != "Billing Ready" and doc.billing_status not in ("Ready", "Held"):
		frappe.throw(_("Billing outcome can only be recorded once the log is Billing Ready."))

	is_manager = bool(set(frappe.get_roles(frappe.session.user)) & {"Log Sheet Manager", "System Manager"})

	if action == "Hold":
		# BR-011: a hold reason is mandatory.
		if not hold_reason:
			frappe.throw(_("A hold reason is mandatory (BR-011)."))
		doc.billing_status = "Held"
		doc.billing_hold_reason = hold_reason
		new_state = doc.workflow_state
	else:
		# BR-011: closing requires a SAP document reference, or a Manager override.
		if not sap_document_reference and not (is_manager and manager_override_reason):
			frappe.throw(_("A manual SAP document reference is required, or a Manager override reason (BR-011)."))
		doc.sap_document_reference = sap_document_reference
		doc.billing_status = "Closed"
		doc.closed_on = now_datetime()
		doc.workflow_state = "Closed"
		new_state = "Closed"

	_log_event(
		doc, "Billing Outcome", "Billing Ready", new_state, action, "Log Sheet Billing User",
		reason_code=hold_reason or manager_override_reason,
		comment=sap_document_reference or hold_reason or manager_override_reason,
	)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"ok": True, "workflow_state": doc.workflow_state, "billing_status": doc.billing_status}
