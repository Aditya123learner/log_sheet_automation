from frappe import _


def get_data():
	return [
		{
			"module_name": "Log Sheet Automation",
			"category": "Modules",
			"label": _("Log Sheet Automation"),
			"color": "#2e7d32",
			"icon": "octicon octicon-checklist",
			"type": "module",
			"description": "Crane log-sheet capture, client approval, Maintenance + SAP parallel validation, Operations sign-off and Billing handoff.",
		}
	]
