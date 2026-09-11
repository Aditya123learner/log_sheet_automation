# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Controller for Equipment Log Sheet — the daily crane log sheet and its
workflow state machine.

This is a straight, cleaned-up port of the "Equipment Log Sheet - Validate"
Server Script that ran this same logic (inside Frappe's RestrictedPython
sandbox) when this app was first prototyped directly on the Desk UI. Running
as normal app code here removes every sandbox workaround that build needed
(see the project's Technical Design document, §9) — this version uses
ordinary imports, f-strings, comprehensions and `frappe.get_roles()` freely.

Design note — workflow_state is DERIVED, not hand-set, except for the three
states that precede client approval (Draft / AI Review / Ready for Client,
set directly by the OCR/link-generation API calls in api.py) and the two
terminal states (Closed / Void). Everything from "Client Approval Pending"
onward is recomputed here on every save from the four gate-status fields, so
the if/elif priority order below *is* the business rule: Closed beats
everything, then client approval, then Maintenance rejection, then SAP
exception, then Operations return, then the "both gates clear" happy path,
else Parallel Validation.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

HOUR_FIELDS = ["working_hours", "idle_hours", "standby_hours", "breakdown_hours", "overtime_hours"]
NUMERIC_MATERIAL_FIELDS = HOUR_FIELDS
TEXT_MATERIAL_FIELDS = ["operating_site", "equipment", "log_date", "shift", "sap_sales_order", "sap_sales_order_item"]

OWNER_MAP = {
	"Draft": "Log Sheet Operator",
	"AI Review": "Log Sheet Operator",
	"Ready for Client": "Log Sheet Operator",
	"Client Approval Pending": "Guest Client Approver",
	"Parallel Validation": "Maintenance + SAP",
	"Operator Rework": "Log Sheet Operator",
	"Sales Action Pending": "Log Sheet Sales Resolver",
	"Operations Review": "Log Sheet Operations Approver",
	"Billing Ready": "Log Sheet Billing User",
	"Closed": "Log Sheet Billing User",
	"Void": "Log Sheet Manager",
}


class EquipmentLogSheet(Document):
	def validate(self):
		self._validate_hour_ranges()
		self._validate_no_duplicate()
		self._revoke_approval_if_material_change()
		self._recompute_workflow_state()
		self.state_entered_on = now_datetime()
		self.current_owner_role = OWNER_MAP.get(self.workflow_state, "")

	# -- BR-002: every hour field must be within [0, 24] --------------------
	def _validate_hour_ranges(self):
		for fieldname in HOUR_FIELDS:
			value = flt(self.get(fieldname))
			if value < 0 or value > 24:
				frappe.throw(_("{0} must be between 0 and 24 hours (BR-002).").format(_(fieldname)))

	# -- BR-004: one log per site + equipment + date + shift ----------------
	def _validate_no_duplicate(self):
		if not (self.operating_site and self.equipment and self.log_date and self.shift):
			return
		duplicate = frappe.db.get_value(
			"Equipment Log Sheet",
			{
				"operating_site": self.operating_site,
				"equipment": self.equipment,
				"log_date": self.log_date,
				"shift": self.shift,
				"workflow_state": ["!=", "Void"],
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_("Duplicate log already exists for this site, equipment, date and shift: {0} (BR-004).").format(
					duplicate
				)
			)

	# -- BR-007: editing a material field after client approval revokes it --
	def _revoke_approval_if_material_change(self):
		if self.is_new() or self.client_approval_status != "Approved":
			return
		if self.flags.get("skip_material_check"):
			return

		material_fields = NUMERIC_MATERIAL_FIELDS + TEXT_MATERIAL_FIELDS
		before = frappe.db.get_value("Equipment Log Sheet", self.name, material_fields, as_dict=True)
		if not before:
			return

		changed = [fn for fn in NUMERIC_MATERIAL_FIELDS if flt(before.get(fn)) != flt(self.get(fn))]
		changed += [fn for fn in TEXT_MATERIAL_FIELDS if str(before.get(fn) or "") != str(self.get(fn) or "")]
		if not changed:
			return

		self.client_approval_status = "Not Requested"
		self.client_token_hash = None
		self.client_token_expiry = None
		self.client_snapshot_hash = None
		self.append(
			"approval_events",
			{
				"event_type": "System",
				"prior_state": self.workflow_state,
				"new_state": "Ready for Client",
				"decision": "Revoked",
				"actor": frappe.session.user,
				"actor_role": "System",
				"event_time": now_datetime(),
				"comment": "Material fields changed after client approval: "
				+ ", ".join(changed)
				+ ". Approval revoked (BR-007).",
			},
		)

	# -- the state machine itself --------------------------------------------
	def _recompute_workflow_state(self):
		if self.workflow_state == "Void":
			return

		if self.billing_status == "Closed":
			self.workflow_state = "Closed"
		elif self.client_approval_status != "Approved":
			if self.client_approval_status == "Rejected":
				self.workflow_state = "Operator Rework"
			elif self.client_approval_status == "Pending":
				self.workflow_state = "Client Approval Pending"
			elif self.client_approval_status == "Expired":
				self.workflow_state = "Ready for Client"
			# "Not Requested": leave workflow_state exactly as the caller set
			# it (Draft / AI Review / Ready for Client are driven directly by
			# api.run_log_sheet_ocr / the operator, not recomputed here).
		elif self.maintenance_status == "Rejected":
			self.workflow_state = "Operator Rework"
		elif self.sap_validation_status in ("Exception", "Failed"):
			# "Failed" (e.g. the live SAP endpoint was unreachable) is routed
			# to the same Sales queue as a business "Exception" — either way
			# a human needs to look at it before the log can proceed; the
			# gate never silently passes just because SAP couldn't be reached.
			self.workflow_state = "Sales Action Pending"
		elif self.operations_status == "Returned":
			self.workflow_state = "Operator Rework"
		elif self.maintenance_status == "Approved" and self.sap_validation_status == "Passed":
			self.workflow_state = "Billing Ready" if self.operations_status == "Approved" else "Operations Review"
		else:
			self.workflow_state = "Parallel Validation"
