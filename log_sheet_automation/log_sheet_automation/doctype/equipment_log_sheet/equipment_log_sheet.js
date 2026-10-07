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
	onload(frm) {
		// A scan with many pages is read in a background job; the server
		// tells this form when it has finished.
		frappe.realtime.off("log_sheet_ocr_done");
		frappe.realtime.on("log_sheet_ocr_done", (data) => {
			if (!data || data.name !== frm.doc.name) return;
			if (data.ok) {
				show_ocr_result(data);
			} else {
				frappe.msgprint({ title: __("OCR failed"), indicator: "red", message: frappe.utils.escape_html(data.error || "") });
			}
			frm.reload_doc();
		});
	},

	setup(frm) {
		// Offer only the contacts of this sheet's Customer as Client Approver.
		frm.set_query("client_approver", () => {
			if (!frm.doc.customer) return {};
			return {
				query: "frappe.contacts.doctype.contact.contact.contact_query",
				filters: { link_doctype: "Customer", link_name: frm.doc.customer },
			};
		});
	},

	operating_site(frm) {
		// A new site brings its own default approver.
		if (!frm.doc.operating_site) return;
		frappe.db.get_value("Operating Site", frm.doc.operating_site, "default_client_approver").then((r) => {
			const approver = r.message && r.message.default_client_approver;
			if (approver) frm.set_value("client_approver", approver);
		});
	},

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
				if (!frm.doc.source_document) {
					frappe.msgprint(__("Attach the scanned log sheet under Source Document first, and save."));
					return;
				}
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the form first, then run OCR."));
					return;
				}
				frappe.call({
					method: "log_sheet_automation.api.run_log_sheet_ocr",
					args: { name: frm.doc.name },
					freeze: true,
					freeze_message: __("Reading the scan with Google Vision..."),
					callback: (r) => {
						if (r.message) show_ocr_result(r.message);
						frm.reload_doc();
					},
				});
			});
		}

		// -- Operator: Send for Client Approval ----------------------------------
		// Generates the one-time approval link, emails it to the Client Approver
		// and moves the sheet to Client Approval Pending.
		if (
			!frm.is_new() &&
			has_role("Log Sheet Operator", "Log Sheet Manager", "System Manager") &&
			!["Closed", "Void", "Client Approval Pending"].includes(frm.doc.workflow_state) &&
			frm.doc.client_approval_status !== "Approved"
		) {
			frm.add_custom_button(__("Send for Client Approval"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the form first, then send it for client approval."));
					return;
				}
				const send = () =>
					frappe.call({
						method: "log_sheet_automation.api.generate_log_sheet_client_link",
						args: { name: frm.doc.name },
						freeze: true,
						freeze_message: __("Sending the approval link..."),
						callback: (r) => {
							if (r.message && r.message.ok) show_client_link(r.message);
							frm.reload_doc();
						},
					});
				if (frm.doc.client_recipient) {
					send();
				} else {
					frappe.confirm(
						__("No Client Approver is selected, so the link cannot be emailed. Create the link anyway and share it yourself?"),
						send
					);
				}
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
			["Pending", "Exception", "Failed"].includes(frm.doc.sap_validation_status)
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
			["Exception", "Failed"].includes(frm.doc.sap_validation_status)
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

		// -- Billing User: Post to SAP / Close manually / Hold ----------------------
		if (
			!frm.is_new() &&
			has_role("Log Sheet Billing User", "Log Sheet Manager", "System Manager") &&
			frm.doc.workflow_state === "Billing Ready"
		) {
			// The normal last step: send the approved sheet and its billable hours
			// to SAP; the SAP document number that comes back closes the sheet.
			if (frm.doc.billing_status !== "Held") {
				const post_button = frm.add_custom_button(
					frm.doc.sap_posting_status === "Failed" ? __("Retry Post to SAP") : __("Post to SAP"),
					() => {
						frappe.confirm(
							__(
								"Post log sheet {0} to SAP?<br><br>Billable hours: <b>{1}</b><br>Sales order: <b>{2}</b> / item <b>{3}</b><br><br>This sends the approved sheet to SAP and closes it. It cannot be posted twice.",
								[
									frm.doc.name,
									frm.doc.billable_hours,
									frappe.utils.escape_html(frm.doc.sap_sales_order || "-"),
									frappe.utils.escape_html(frm.doc.sap_sales_order_item || "-"),
								]
							),
							() => {
								frappe.call({
									method: "log_sheet_automation.api.post_log_sheet_to_sap",
									args: { name: frm.doc.name },
									freeze: true,
									freeze_message: __("Posting to SAP..."),
									callback: (r) => {
										const result = r.message || {};
										frappe.msgprint({
											title: result.ok ? __("Posted to SAP") : __("SAP posting failed"),
											indicator: result.ok ? "green" : "red",
											message: result.ok
												? __("SAP document <b>{0}</b> was created and the log sheet is closed.", [
														frappe.utils.escape_html(result.sap_document_reference || ""),
												  ])
												: __("{0}<br><br>The log sheet is still Billing Ready. Fix the cause and use Retry Post to SAP.", [
														frappe.utils.escape_html(result.message || ""),
												  ]),
										});
										frm.reload_doc();
									},
								});
							}
						);
					}
				);
				post_button.removeClass("btn-default").addClass("btn-primary");
			}

			// Fallback when SAP cannot be posted to: type the SAP reference by hand.
			frm.add_custom_button(__("Close Billing Manually"), () => {
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


// After "Send for Client Approval": who it was emailed to (or why not), and
// the FULL link — clickable and easy to copy — in case it has to be shared
// by another route. The link is shown this one time only.
function show_client_link(result) {
	const url = result.approval_url || frappe.urllib.get_full_url(result.approval_path);
	const safe_url = frappe.utils.escape_html(url);
	const emailed = (result.notification || "").toLowerCase().includes("emailed");
	frappe.msgprint({
		title: __("Sent for Client Approval"),
		indicator: emailed ? "green" : "orange",
		message:
			`<p><b>${frappe.utils.escape_html(result.notification || "")}</b></p>` +
			`<p>${__("Approval link (valid once, until {0}):", [frappe.datetime.str_to_user(result.expiry)])}</p>` +
			`<p><a href="${safe_url}" target="_blank" rel="noopener">${safe_url}</a></p>` +
			`<p class="text-muted small">${__("Copy it now if you need it: for security the link is not stored and cannot be shown again. Sending again creates a new link and cancels this one.")}</p>`,
	});
}

// One line per page of the scan: which Equipment Log Sheet it became, how
// many Daily Log rows were read, and whether anything needs checking.
function show_ocr_result(result) {
	if (result.queued) {
		frappe.msgprint({
			title: __("OCR started"),
			indicator: "blue",
			message: __("This scan has {0} pages, so it is being read in the background. This form will refresh when it is done.", [result.pages]),
		});
		return;
	}
	const sheets = result.sheets || [];
	const lines = sheets.map((sheet) => {
		if (sheet.skipped) {
			return __("Page {0}: skipped — {1}", [sheet.page_no, frappe.utils.escape_html(sheet.skipped)]);
		}
		const link = `<a href="/app/equipment-log-sheet/${encodeURIComponent(sheet.name)}">${frappe.utils.escape_html(sheet.name)}</a>`;
		const review = sheet.low_confidence ? __("needs review") : __("read cleanly");
		return __("Page {0}: {1} — {2} sheet, {3} row(s), {4}", [sheet.page_no, link, sheet.template || __("unrecognised"), sheet.rows, review]);
	});
	const needs_review = sheets.some((sheet) => sheet.skipped || sheet.low_confidence);
	frappe.msgprint({
		title: __("OCR complete"),
		indicator: needs_review ? "orange" : "green",
		message:
			lines.join("<br>") +
			(needs_review
				? "<br><br>" + __("Check the rows marked Exception under Validation Results, correct the Daily Log, then tick OCR Review Complete.")
				: ""),
	});
}
