# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Whitelisted API surface for Log Sheet Automation.

Nine endpoints, matching the SRS's Appendix B "controller methods". Two are
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
# 1. OCR (Mock or Azure Document Intelligence, per Settings)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def run_log_sheet_ocr(name):
	"""Run OCR extraction against a Draft/AI Review log sheet. Dispatches to
	the Mock or Azure provider per `Log Sheet Automation Settings.ocr_provider_mode`
	— see log_sheet_automation/integrations/ocr.py."""
	_require_role("Log Sheet Operator", "Log Sheet Manager", "System Manager")

	doc = frappe.get_doc("Equipment Log Sheet", name)
	if doc.workflow_state not in ("Draft", "AI Review"):
		frappe.throw(_("OCR can only be run while the log is in Draft or AI Review."))

	settings = _get_settings()
	threshold = flt(settings.mock_ocr_confidence_low_threshold) or 0.75

	run_id, extracted = ocr_integration.extract(doc, settings)

	low_confidence = False
	for row in extracted:
		doc.set(row["field"], row["value"])
		status = "Passed" if row["confidence"] >= threshold else "Exception"
		low_confidence = low_confidence or status == "Exception"
		doc.append(
			"validation_table",
			{
				"validation_type": "OCR",
				"run_id": run_id,
				"check_code": row["field"],
				"status": status,
				"message": f"Extracted {row['value']} with confidence {row['confidence']}",
				"input_summary": json.dumps({"field": row["field"], "confidence": row["confidence"]}),
				"source_timestamp": now_datetime(),
				"provider_mode": settings.ocr_provider_mode,
				"is_current": 1,
			},
		)

	doc.ocr_status = "Completed"
	doc.ocr_provider_result_id = run_id
	doc.ocr_review_complete = 0 if low_confidence else 1
	doc.workflow_state = "AI Review"

	_log_event(
		doc, "System", "Draft", "AI Review", "OCR Completed", "Log Sheet Operator",
		comment=f"{settings.ocr_provider_mode} OCR run {run_id}. Low-confidence fields require review: {low_confidence}",
	)

	doc.save()
	return {"ok": True, "run_id": run_id, "low_confidence": low_confidence, "workflow_state": doc.workflow_state}


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
	missing = [fn for fn in ("operating_site", "equipment", "log_date") if not doc.get(fn)]
	if not doc.operator_user:
		missing.append("operator_user")
	if missing:
		frappe.throw(_("Cannot generate client link, missing mandatory fields: {0} (BR-001).").format(", ".join(missing)))

	# BR-006: low-confidence OCR fields must be confirmed first
	if doc.ocr_status == "Completed" and not doc.ocr_review_complete:
		frappe.throw(_("Confirm low-confidence OCR fields before generating the client link (BR-006)."))

	# BR-005: an active commercial mapping must exist for this site/equipment/date
	mapping = frappe.get_all(
		"Site Equipment Commercial Mapping",
		filters={
			"operating_site": doc.operating_site,
			"equipment": doc.equipment,
			"is_active": 1,
			"valid_from": ["<=", doc.log_date],
		},
		fields=["name", "sap_sales_order", "sap_sales_order_item", "work_order_reference", "uom", "rate_key", "billing_rule"],
		order_by="valid_from desc",
		limit_page_length=1,
	)
	if not mapping:
		frappe.throw(_("No active Site Equipment Commercial Mapping for this site/equipment/date (BR-005)."))

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
		"log_date": str(doc.log_date),
		"shift": doc.shift,
		"operating_site": doc.operating_site,
		"equipment": doc.equipment,
		"working_hours": doc.working_hours,
		"idle_hours": doc.idle_hours,
		"standby_hours": doc.standby_hours,
		"breakdown_hours": doc.breakdown_hours,
		"overtime_hours": doc.overtime_hours,
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
		"log_date": str(doc.log_date),
		"shift": doc.shift,
		"working_hours": doc.working_hours,
		"idle_hours": doc.idle_hours,
		"standby_hours": doc.standby_hours,
		"breakdown_hours": doc.breakdown_hours,
		"overtime_hours": doc.overtime_hours,
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
