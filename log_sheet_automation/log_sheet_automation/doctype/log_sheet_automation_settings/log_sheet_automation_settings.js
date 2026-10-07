// Copyright (c) 2026, Logic Motive Consultant and contributors
// For license information, please see license.txt

frappe.ui.form.on("Log Sheet Automation Settings", {
	setup(frm) {
		// The key file Google Cloud gives you is a .json file — only offer those.
		frm.get_field("google_service_account_key_file").df.options = {
			restrictions: { allowed_file_types: [".json", "application/json"] },
		};
	},

	refresh(frm) {
		if (!frm.doc.google_key_status) {
			frm.set_intro(
				__("No Google key has been loaded yet. Upload the .json key file under Google Service Account Key File and click Save."),
				"orange"
			);
		}

		// One tiny request that proves the whole chain: the saved key is a real
		// service account key -> Google issues a token -> the Vision API is
		// enabled (with billing) and answers. Saves finding out at OCR time.
		frm.add_custom_button(__("Test Google Vision Connection"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the settings first, then test the connection."));
				return;
			}
			frappe.call({
				method: "log_sheet_automation.api.test_ocr_connection",
				freeze: true,
				freeze_message: __("Calling Google Vision..."),
				callback: (r) => {
					if (!r.message || !r.message.ok) return;
					frappe.msgprint({
						title: __("Google Vision is working"),
						indicator: "green",
						message: __(
							"Project: <b>{0}</b><br>Service account: {1}<br>Test image was read as: <code>{2}</code>",
							[
								frappe.utils.escape_html(r.message.project_id || ""),
								frappe.utils.escape_html(r.message.client_email || ""),
								frappe.utils.escape_html(r.message.recognised_text || ""),
							]
						),
					});
				},
			});
		});
	},
});
