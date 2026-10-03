# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Controller for Equipment Log Sheet — the crane log sheet (one record per
Weekly or Monthly physical sheet, with a Daily Log child-table row per day)
and its workflow state machine.

This is a straight, cleaned-up port of the "Equipment Log Sheet - Validate"
Server Script that ran this same logic (inside Frappe's RestrictedPython
sandbox) when this app was first prototyped directly on the Desk UI. Running
as normal app code here removes every sandbox workaround that build needed
(see the project's Technical Design document, §9) — this version uses
ordinary imports, f-strings, comprehensions and `frappe.get_roles()` freely.

Schema note — this DocType was restructured from "one record per single
day+shift" to "one record per Weekly/Monthly sheet, with a Daily Log row per
day" after the real Sanghvi Movers paper log sheets were reviewed: neither
the Weekly nor the Monthly template is a single-day form, and neither tracks
"idle hours" / "standby hours" as distinct values. `working_hours`,
`breakdown_hours` and `overtime_hours` are now header-level fields COMPUTED
by summing the Daily Log (`daily_rows`) rows on every save — see
`_recompute_aggregates` below — rather than being entered directly, so every
downstream consumer that already read them as plain scalars (the client
approval snapshot, the billing calculation in api.py) keeps working
unchanged. `idle_hours` / `standby_hours` are kept on the schema (so nothing
downstream that references them breaks) but are always forced to 0, per the
confirmed decision that neither real template tracks that distinction.

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

import json

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime, sha256_hash
from frappe.model.document import Document

DAILY_HOUR_FIELDS = ["total_hours", "normal_shift_hours", "overtime_hours", "breakdown_hours"]

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
		self._recompute_aggregates()
		self._revoke_approval_if_material_change()
		self._recompute_workflow_state()
		self.state_entered_on = now_datetime()
		self.current_owner_role = OWNER_MAP.get(self.workflow_state, "")

	# -- BR-002: every per-day hour field must be within [0, 24] -------------
	def _validate_hour_ranges(self):
		if self.period_start_date and self.period_end_date and getdate(self.period_end_date) < getdate(
			self.period_start_date
		):
			frappe.throw(_("Period End Date cannot be before Period Start Date."))

		for row in self.daily_rows:
			for fieldname in DAILY_HOUR_FIELDS:
				value = flt(row.get(fieldname))
				if value < 0 or value > 24:
					frappe.throw(
						_("Daily Log row {0} ({1}): {2} must be between 0 and 24 hours (BR-002).").format(
							row.idx, row.log_date or "", _(fieldname)
						)
					)

	# -- BR-004: one log per site + equipment + template + period ------------
	def _validate_no_duplicate(self):
		if not (self.operating_site and self.equipment and self.sheet_template and self.period_start_date):
			return
		duplicate = frappe.db.get_value(
			"Equipment Log Sheet",
			{
				"operating_site": self.operating_site,
				"equipment": self.equipment,
				"sheet_template": self.sheet_template,
				"period_start_date": self.period_start_date,
				"workflow_state": ["!=", "Void"],
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"Duplicate log already exists for this site, equipment, template and period "
					"(BR-004): {0}."
				).format(duplicate)
			)

	# -- Header totals are computed from the Daily Log, not entered directly -
	def _recompute_aggregates(self):
		self.working_hours = sum(flt(row.total_hours) for row in self.daily_rows)
		self.breakdown_hours = sum(flt(row.breakdown_hours) for row in self.daily_rows)
		self.overtime_hours = sum(flt(row.overtime_hours) for row in self.daily_rows)
		# Neither real log sheet template tracks idle/standby time as a
		# distinct value — these fields are kept on the schema (so nothing
		# that already reads them as plain scalars needs to change) but are
		# always forced to 0, per the confirmed decision.
		self.idle_hours = 0
		self.standby_hours = 0

	# -- BR-007: editing a material field after client approval revokes it --
	def _revoke_approval_if_material_change(self):
		if self.is_new() or self.client_approval_status != "Approved":
			return
		if self.flags.get("skip_material_check"):
			return
		if not self.approved_content_hash:
			return

		current_hash = self.compute_content_hash()
		if current_hash == self.approved_content_hash:
			return

		self.client_approval_status = "Not Requested"
		self.client_token_hash = None
		self.client_token_expiry = None
		self.client_snapshot_hash = None
		self.approved_content_hash = None
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
				"comment": "Header fields or Daily Log rows changed after client approval. "
				"Approval revoked (BR-007).",
			},
		)

	def compute_content_hash(self):
		"""Canonical hash over the header identity fields and every Daily Log
		row's content. Computed and stored as `approved_content_hash` the
		moment the client approves (see api.record_log_sheet_client_decision);
		`_revoke_approval_if_material_change` recomputes it on every later
		save and compares — any drift, including an edit inside the Daily Log
		child table, means something material changed since approval. A
		two-query DB diff (the approach used before this DocType had a child
		table) can't easily detect a child-table edit, which is why this
		compares against a hash captured at approval time instead."""
		payload = {
			"operating_site": self.operating_site,
			"equipment": self.equipment,
			"sheet_template": self.sheet_template,
			"period_start_date": str(self.period_start_date) if self.period_start_date else None,
			"period_end_date": str(self.period_end_date) if self.period_end_date else None,
			"rows": [
				{
					"log_date": str(row.log_date) if row.log_date else None,
					"from_time": str(row.from_time) if row.from_time else None,
					"to_time": str(row.to_time) if row.to_time else None,
					"total_hours": flt(row.total_hours),
					"normal_shift_hours": flt(row.normal_shift_hours),
					"overtime_hours": flt(row.overtime_hours),
					"breakdown_hours": flt(row.breakdown_hours),
				}
				for row in sorted(self.daily_rows, key=lambda r: (str(r.log_date or ""), r.idx))
			],
		}
		return sha256_hash(json.dumps(payload, sort_keys=True))

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
