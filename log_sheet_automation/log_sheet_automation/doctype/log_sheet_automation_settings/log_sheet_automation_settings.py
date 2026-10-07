# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from log_sheet_automation.integrations.ocr import parse_service_account_key


class LogSheetAutomationSettings(Document):
	def validate(self):
		self._load_google_key_from_file()
		self._validate_google_key()

	def on_update(self):
		self._delete_uploaded_key_file()

	def _load_google_key_from_file(self):
		"""The Google key is supplied by UPLOADING the .json file Google Cloud
		hands out, not by pasting it. On save: read the upload, check it is a
		real service account key, move its contents into the encrypted
		Password field, and forget the upload (the file itself is deleted in
		on_update, once the save has actually gone through).

		Why not a visible Password box to paste into: browsers treat it as a
		login field and auto-fill it — a 10-character saved password was once
		stored there in place of the ~2,300-character key, and nothing
		complained until OCR was run."""
		file_url = self.google_service_account_key_file
		if not file_url:
			return

		file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
		if not file_name:
			frappe.throw(_("The uploaded Google key file could not be found. Upload it again."))
		content = frappe.get_doc("File", file_name).get_content()
		if isinstance(content, bytes):
			try:
				content = content.decode("utf-8-sig")
			except UnicodeDecodeError:
				frappe.throw(_("The uploaded file is not a text .json file. Upload the key file exactly as downloaded from Google Cloud."))

		try:
			info = parse_service_account_key(content)
		except ValueError as e:
			frappe.throw(str(e), title=_("Google Service Account Key File"))

		self.google_service_account_key = content.strip()
		self.google_key_status = _("{0} (project {1}), key ID ending {2}, loaded {3}").format(
			info.get("client_email"),
			info.get("project_id") or "?",
			(info.get("private_key_id") or "")[-6:] or "?",
			now_datetime().strftime("%d-%m-%Y %H:%M"),
		)
		self.google_service_account_key_file = None
		self.flags.uploaded_key_file_url = file_url

	def _validate_google_key(self):
		"""Whatever ends up in the stored key must be a real service account
		key. An unchanged, already-saved key arrives here masked as asterisks
		and is left alone."""
		value = (self.google_service_account_key or "").strip()
		if not value or set(value) == {"*"}:
			return
		try:
			parse_service_account_key(value)
		except ValueError as e:
			frappe.throw(str(e), title=_("Google Service Account Key"))

	def _delete_uploaded_key_file(self):
		file_url = self.flags.get("uploaded_key_file_url")
		if not file_url:
			return
		for name in frappe.get_all("File", filters={"file_url": file_url}, pluck="name"):
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
