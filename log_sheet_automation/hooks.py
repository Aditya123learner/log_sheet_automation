app_name = "log_sheet_automation"
app_title = "Log Sheet Automation"
app_publisher = "Logic Motive Consultant"
app_description = "Daily crane log-sheet automation: capture, OCR, client approval, parallel Maintenance + SAP validation, Operations sign-off and Billing handoff for Sanghvi Movers."
app_email = "erpnext@logic-motive.com"
app_license = "MIT"
app_version = "0.1.0"

# This app links to ERPNext's own "Customer" DocType (Equipment Log Sheet's
# client_recipient lookup, Operating Site's billing customer), so ERPNext
# must already be installed on the site before this app is — bench reads
# this list before install, install-app, and migrate.
required_apps = ["erpnext"]

# Includes in <head>
# ------------------
# include js, css files in header of desk.html
# app_include_css = "/assets/log_sheet_automation/css/log_sheet_automation.css"
# app_include_js = "/assets/log_sheet_automation/js/log_sheet_automation.js"

# Home Pages
# ----------
# application home page (will override Website Settings)
# home_page = "login"

# Website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------
# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Equipment Log Sheet owns its full validate()/state-machine logic directly
# on its controller class (see doctype/equipment_log_sheet/equipment_log_sheet.py)
# per standard Frappe convention — no doc_events indirection needed since
# this app owns that DocType. doc_events is reserved here for any future
# hook into a DocType owned by another app (e.g. Asset, Customer).
doc_events = {}

# Scheduled Tasks
# ---------------
# scheduler_events = {
# 	"all": [
# 		"log_sheet_automation.tasks.all"
# 	],
# }

# Fixtures
# --------
# Exported once via `bench --site <site> export-fixtures` and re-applied on
# every `bench migrate` so a fresh site gets the roles/workspace/print format
# / number cards this app ships with.

fixtures = [
	{"dt": "Role", "filters": [["name", "like", "Log Sheet %"]]},
	{"dt": "Workspace", "filters": [["module", "=", "Log Sheet Automation"]]},
	{"dt": "Print Format", "filters": [["module", "=", "Log Sheet Automation"]]},
	{"dt": "Number Card", "filters": [["name", "like", "LSA - %"]]},
]

# Website Route Rules
# --------------------
# No explicit rule needed: the public, no-login client approval page is
# served automatically from log_sheet_automation/www/log-sheet-approval.html
# (+ .py context) at the route /log-sheet-approval, per Frappe's standard
# www/ folder convention.

# Jinja
# ----------------------
# add methods and filters to jinja environment
# jinja = {
# 	"methods": [],
# 	"filters": []
# }

# Installation
# ------------
# before_install = "log_sheet_automation.install.before_install"
# after_install = "log_sheet_automation.install.after_install"

# Uninstallation
# ------------
# before_uninstall = "log_sheet_automation.uninstall.before_uninstall"
# after_uninstall = "log_sheet_automation.uninstall.after_uninstall"

# Permissions
# -----------
# Point of entry for finer-grained permission control beyond the DocType
# permission matrix (kept in each DocType's .json — see the Technical
# Design document for the full role/permission table).
# permission_query_conditions = {
# 	"Equipment Log Sheet": "log_sheet_automation.log_sheet_automation.doctype.equipment_log_sheet.equipment_log_sheet.get_permission_query_conditions",
# }
