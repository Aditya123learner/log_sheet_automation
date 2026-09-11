// Copyright (c) 2026, Logic Motive Consultant and contributors
// For license information, please see license.txt

// Role- and workflow-state-conditional action buttons for Equipment Log
// Sheet. Every button here POSTs to a whitelisted method in api.py and then
// reloads the form — note that role gating here is UX only: each action is
// re-guarded server-side inside its own api.py function, which is where the
// real access control lives (see Technical Design document §7).

const STATE_COLOR = {
	"Draft": "grey",
	"AI Review": "orange",
	"Ready for Client": "blue",
	"Client Approval Pending": "yellow",
	"Parallel Validation": "blue",
	"Operator Rework": "red",
	"Sales Action Pending": "orange",
	"Operations Review": "blue",
	"Billing Ready": "green",
	"Closed": "green",
	"Void": "grey",
};

frappe.ui.form.on("Equipment Log Sheet", {
	refresh(frm) {
		if (frm.doc.workflow_state) {
			frm.dashboard.add_indicator(
				__("State: {0}", [frm.doc.workflow_state]),
				STATE_COLOR[frm.doc.workflow_state] || "grey"
			);
		}

		const roles = frappe.user_roles;
		const has_role = (...allowed) => allowed.some((r) => roles.includes(r));

		// -- Operator: Run OCR ------------------------------------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Operator", "Log Sheet Manager", "System Manager") &&
			["Draft", "AI Review"].includes(frm.doc.workflow_state)
		) {
			frm.add_custom_button(__("Run OCR"), () => {
				frappe.call({
					method: "log_sheet_automation.api.run_log_sheet_ocr",
					args: { name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		}

		// -- Operator: Generate Client Approval Link ---------------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Operator", "Log Sheet Manager", "System Manager") &&
			!["Closed", "Void", "Client Approval Pending"].includes(frm.doc.workflow_state) &&
			frm.doc.client_approval_status !== "Approved"
		) {
			frm.add_custom_button(__("Generate Client Approval Link"), () => {
				frappe.call({
					method: "log_sheet_automation.api.generate_log_sheet_client_link",
					args: { name: frm.doc.name },
					freeze: true,
					callback: (r) => {
						if (r.message && r.message.ok) {
							frappe.msgprint({
								title: __("Client Approval Link"),
								message: __(
									"Share this link with the client (expires {0}):<br><code>{1}</code>",
									[r.message.expiry, r.message.approval_path]
								),
								indicator: "green",
							});
						}
						frm.reload_doc();
					},
				});
			});
		}

		// -- Maintenance Approver: Record Maintenance Decision -----------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Maintenance Approver", "Log Sheet Manager", "System Manager") &&
			frm.doc.maintenance_status === "Pending"
		) {
			frm.add_custom_button(__("Record Maintenance Decision"), () => {
				prompt_maintenance_decision(frm);
			});
		}

		// -- Operations/Sales: Run SAP Validation -------------------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Operations Approver", "Log Sheet Sales Resolver", "Log Sheet Manager", "System Manager") &&
			["Pending", "Exception"].includes(frm.doc.sap_validation_status)
		) {
			frm.add_custom_button(__("Run SAP Validation"), () => {
				frappe.call({
					method: "log_sheet_automation.api.run_log_sheet_sap_validation",
					args: { name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		}

		// -- Sales Resolver: Request SAP Revalidation ---------------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Sales Resolver", "Log Sheet Manager", "System Manager") &&
			frm.doc.sap_validation_status === "Exception"
		) {
			frm.add_custom_button(__("Request SAP Revalidation"), () => {
				frappe.prompt(
					[{ fieldname: "comment", fieldtype: "Small Text", label: __("Corrective Action"), reqd: 1 }],
					(values) => {
						frappe.call({
							method: "log_sheet_automation.api.request_log_sheet_sap_revalidation",
							args: { name: frm.doc.name, comment: values.comment },
							freeze: true,
							callback: () => frm.reload_doc(),
						});
					},
					__("Request SAP Revalidation")
				);
			});
		}

		// -- Operations Approver: Approve / Return -------------------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Operations Approver", "Log Sheet Manager", "System Manager") &&
			frm.doc.workflow_state === "Operations Review"
		) {
			frm.add_custom_button(__("Approve (Operations)"), () => {
				frappe.confirm(__("Approve this log sheet? Billable hours will be calculated."), () => {
					frappe.call({
						method: "log_sheet_automation.api.record_log_sheet_operations_decision",
						args: { name: frm.doc.name, decision: "Approved" },
						freeze: true,
						callback: () => frm.reload_doc(),
					});
				});
			}, __("Operations"));

			frm.add_custom_button(__("Return (Operations)"), () => {
				frappe.prompt(
					[{ fieldname: "comment", fieldtype: "Small Text", label: __("Reason for Return"), reqd: 1 }],
					(values) => {
						frappe.call({
							method: "log_sheet_automation.api.record_log_sheet_operations_decision",
							args: { name: frm.doc.name, decision: "Returned", comment: values.comment },
							freeze: true,
							callback: () => frm.reload_doc(),
						});
					},
					__("Return for Rework")
				);
			}, __("Operations"));
		}

		// -- Billing User: Close / Hold -------------------------------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Billing User", "Log Sheet Manager", "System Manager") &&
			frm.doc.workflow_state === "Billing Ready"
		) {
			frm.add_custom_button(__("Close Billing"), () => {
				const is_manager = has_role("Log Sheet Manager", "System Manager");
				const fields = [
					{ fieldname: "sap_document_reference", fieldtype: "Data", label: __("SAP Document Reference") },
				];
				if (is_manager) {
					fields.push({
						fieldname: "manager_override_reason",
						fieldtype: "Small Text",
						label: __("Manager Override Reason (only if no SAP reference)"),
					});
				}
				frappe.prompt(fields, (values) => {
					frappe.call({
						method: "log_sheet_automation.api.record_log_sheet_billing_outcome",
						args: {
							name: frm.doc.name,
							action: "Close",
							sap_document_reference: values.sap_document_reference,
							manager_override_reason: values.manager_override_reason,
						},
						freeze: true,
						callback: () => frm.reload_doc(),
					});
				}, __("Close Billing"));
			}, __("Billing"));

			frm.add_custom_button(__("Hold Billing"), () => {
				frappe.prompt(
					[{ fieldname: "hold_reason", fieldtype: "Small Text", label: __("Hold Reason"), reqd: 1 }],
					(values) => {
						frappe.call({
							method: "log_sheet_automation.api.record_log_sheet_billing_outcome",
							args: { name: frm.doc.name, action: "Hold", hold_reason: values.hold_reason },
							freeze: true,
							callback: () => frm.reload_doc(),
						});
					},
					__("Hold Billing")
				);
			}, __("Billing"));
		}
	},
});

function prompt_maintenance_decision(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Record Maintenance Decision"),
		fields: [
			{
				fieldname: "decision",
				fieldtype: "Select",
				label: __("Decision"),
				options: "Approved\nRejected",
				reqd: 1,
			},
			{
				fieldname: "reason_code",
				fieldtype: "Select",
				label: __("Reason Code"),
				options: "\nMechanical\nElectrical\nHydraulic\nOperator\nDocumentation\nOther",
				depends_on: "eval:doc.decision=='Rejected'",
			},
			{
				fieldname: "comment",
				fieldtype: "Small Text",
				label: __("Comment"),
				depends_on: "eval:doc.decision=='Rejected'",
			},
		],
		primary_action_label: __("Submit"),
		primary_action(values) {
			if (values.decision === "Rejected" && (!values.reason_code || !values.comment)) {
				frappe.msgprint(__("Reason code and comment are mandatory on rejection (BR-008)."));
				return;
			}
			frappe.call({
				method: "log_sheet_automation.api.record_log_sheet_maintenance_decision",
				args: {
					name: frm.doc.name,
					decision: values.decision,
					reason_code: values.reason_code,
					comment: values.comment,
				},
				freeze: true,
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}
