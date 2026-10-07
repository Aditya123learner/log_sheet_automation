# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

from frappe.model.document import Document


# The class name must be the DocType name with the spaces removed, capitals
# kept exactly: "Log Sheet OCR Template" -> LogSheetOCRTemplate. With the old
# spelling (LogSheetOcrTemplate) Frappe could not find the controller, treated
# the DocType as orphaned and deleted it on every `bench migrate`, which broke
# the Equipment Log Sheet form ("ocr_template is referring to non-existing
# doctype"). The table and its records are not touched by that; they
# reappear once this DocType is synced again.
class LogSheetOCRTemplate(Document):
	pass
