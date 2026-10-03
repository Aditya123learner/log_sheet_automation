# Log Sheet Automation — Technical Design Document

**Site:** lmcpl.erpnext.com &nbsp;|&nbsp; **Framework:** Frappe / ERPNext v15 &nbsp;|&nbsp; **Module:** Log Sheet Automation &nbsp;|&nbsp; **Build method:** In\-app Desk UI customization

Table of Contents

## 1\. Architecture & build method

This MVP was built **directly on the production Desk UI** using Frappe's native customization primitives — Custom DocTypes, Server Scripts, Client Scripts, a Workspace, a Print Format — rather than as a separate installed, git\-tracked custom app. That was a deliberate scope decision for a same\-day build with browser\-only access (no bench/SSH). Everything below is real, live, working configuration on the site; there is no separate codebase.

**What this means in practice:**

- All business logic lives in **Server Script** documents (Python, executed inside Frappe's `safe_exec` / RestrictedPython sandbox) rather than in an installed app's `.py` controller files.
- Client\-side behaviour lives in a single **Client Script** document rather than a bundled `.js` file.
- Everything is exportable via **Customize Form → Export Customizations** / `bench export-fixtures`\-style tooling, or can be reverse\-engineered into a proper app using `bench get-doctype`/manual scaffolding, if/when this is promoted to a git\-tracked app (recommended before scaling past demo use — see §10).
- Because Server Scripts run in a restricted sandbox, several standard Frappe/Python idioms are **not available** and had to be worked around — documented in §9, since the next person editing this logic will hit the same walls.

## 2\. Module & DocType inventory

Module Def: **Log Sheet Automation**.

| DocType | Type | Purpose |
| --- | --- | --- |
| Operating Site | Master | Site/yard master, linked to Customer |
| Log Sheet OCR Template | Master | OCR layout definition |
| Log Sheet Billing Rule | Master | Billable\-hours calculation rule |
| Site Equipment Commercial Mapping | Master | Equipment ↔ SAP commercial reference |
| Log Sheet Automation Settings | Single | Integration mode \+ secrets \+ tunables |
| Equipment Log Breakdown | Child table | Breakdown intervals on a log sheet |
| Equipment Log Validation | Child table | OCR/SAP/Business check results |
| Equipment Log Approval Event | Child table | Immutable audit trail |
| Equipment Log Sheet Day | Child table | One day's row within a Weekly/Monthly log sheet |
| **Equipment Log Sheet** | **Main transaction** | The Weekly/Monthly log sheet \+ workflow state machine |

> **Schema note (2026\-10):** Equipment Log Sheet was restructured after the real Sanghvi Movers paper log sheets were reviewed — see §4 below. It moved from "one record per single day\+shift" to "one record per Weekly/Monthly physical sheet, with a Daily Log child\-table row (`Equipment Log Sheet Day`) per day." The rest of this document was written against the original one\-record\-per\-day shape and has been updated where it matters (§4, §5.1, §5.3); §9's RestrictedPython discoveries are historical and left as\-is.

## 3\. Roles

Seven custom roles, all with Desk access: `Log Sheet Operator`, `Log Sheet Maintenance Approver`, `Log Sheet Sales Resolver`, `Log Sheet Operations Approver`, `Log Sheet Billing User`, `Log Sheet Manager`, `Log Sheet Auditor`.

### 3\.1 DocType permission matrix

`r`\=read, `w`\=write, `c`\=create, `d`\=delete. Blank \= no access. Child tables (`istable=1`) carry no standalone permission rows — access follows the parent document.

| DocType | System Manager | LS Manager | LS Operator | LS Maintenance | LS Sales | LS Operations | LS Billing | LS Auditor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operating Site | rwcd | rwcd | r | r | r | rwc | r | r |
| Log Sheet OCR Template | rwcd | rwcd | r | – | – | rwc | – | r |
| Log Sheet Billing Rule | rwcd | rwcd | r | – | – | rwc | r | r |
| Site Equipment Commercial Mapping | rwcd | rwcd | r | – | r | rwc | r | r |
| Log Sheet Automation Settings (Single) | rwcd | rwc | – | – | – | – | – | – |
| **Equipment Log Sheet** | rwcd | rwcd | rwc | r | r | r | r | r |

Note: DocType\-level permissions only gate direct document read/write/create/delete (e.g. via the list view or REST). The *workflow actions* (approve, reject, close billing, etc.) are enforced independently and more granularly inside each Server Script — see §5.2 — via explicit role checks, so a role with only `r` on Equipment Log Sheet can still be the one whitelisted to flip a status field via its dedicated API method (running with `ignore_permissions=True` inside the script after the role check passes).

## 4\. Equipment Log Sheet — full field schema

`autoname = naming_series:` → series `LS-.YYYY.-.#####` (e.g. `LS-2026-00003`). Not submittable (`is_submittable=0`); soft\-deleted state is instead represented by `workflow_state = "Void"`.

```text
workflow_state          Select   RO   Draft|AI Review|Ready for Client|Client Approval Pending|
                                       Parallel Validation|Operator Rework|Sales Action Pending|
                                       Operations Review|Billing Ready|Closed|Void
current_owner_role      Data     RO
state_entered_on        Datetime RO

--- Section: Header ---
naming_series            Select   LS-.YYYY.-.#####
sheet_template            Select   Weekly | Monthly                  required  (which physical template)
log_sheet_no              Data     (printed sheet number — Weekly only prints one)
month                      Data     required  (as printed, e.g. "September 2026")
period_start_date         Date     required
period_end_date           Date     required
operating_site            Link     Operating Site                    required
customer                   Link     Customer                          RO (fetched)
equipment                  Link     Asset                             required
operator_user             Link     User

--- Section: Source Document / OCR ---
source_document           Attach
ocr_template               Link     Log Sheet OCR Template
ocr_status                 Select   Not Run|Queued|Completed|Failed   RO
ocr_provider_result_id    Data     RO
ocr_review_complete       Check

--- Section: Daily Log ---
daily_rows                 Table    Equipment Log Sheet Day   — one row per day; this is what OCR fills

--- Section: Utilization (computed from Daily Log on every save) ---
working_hours / breakdown_hours / overtime_hours   Float  RO  (summed from daily_rows)
idle_hours / standby_hours                            Float  RO  default 0 (neither real template tracks these — kept on the schema, always forced to 0)
operator_remarks          Small Text

--- Section: Breakdown Incident Detail (separate from the per-day breakdown_hours total) ---
breakdown_table            Table    Equipment Log Breakdown

--- Section: Commercial Mapping (fetched from Site Equipment Commercial Mapping, RO on the sheet) ---
sap_sales_order / sap_sales_order_item / work_order_reference / uom / rate_key   Data   RO

--- Section: Client Approval ---
client_recipient           Data
client_approval_status    Select   Not Requested|Pending|Approved|Rejected|Expired   RO
client_decision_on        Datetime RO
client_token_hash         Data     RO   (SHA-256 of the opaque token — token itself never stored)
client_token_expiry       Datetime RO
client_snapshot_hash      Data     RO   (SHA-256 of the canonical JSON the client saw, incl. daily_rows)
approved_content_hash    Data     RO   (SHA-256 of header identity fields + every Daily Log row, captured at approval — BR-007's baseline)
client_comment             Small Text

--- Section: Parallel Validation ---
maintenance_status         Select   Not Started|Pending|Approved|Rejected            RO
maintenance_reason_code   Select   (blank)|Mechanical|Electrical|Hydraulic|Operator|Documentation|Other
maintenance_comment        Small Text
sap_validation_status     Select   Not Started|Pending|Passed|Exception|Failed        RO
sap_current_run_id        Data     RO
operations_status          Select   Not Started|Pending|Approved|Returned              RO
operations_comment         Small Text
validation_table           Table    Equipment Log Validation

--- Section: Calculation ---
billing_rule                Link     Log Sheet Billing Rule
billing_rule_version       Data     RO
billable_hours              Float    RO
calculation_trace          Code (JSON)  RO

--- Section: Billing ---
billing_status              Select   Not Ready|Ready|Held|Closed   RO
billing_hold_reason        Small Text
sap_document_reference     Data
closed_on                    Datetime RO

--- Section: Sheet Sign-off (as printed; record-keeping only, no workflow logic reads these) ---
hour_meter_opening / hour_meter_closing      Float   (Weekly only)
total_working_days_words                      Data
total_overtime_hours_words                    Data
total_breakdown_hours_words                   Data   (Weekly only)
site_engineer_name                             Data
mechanical_hod_name                            Data   (Monthly only)
project_manager_name                           Data

--- Section: Audit ---
approval_events             Table    Equipment Log Approval Event   RO
```

### 4\.1 Child DocTypes

**Equipment Log Sheet Day** (`istable=1`): `day_label` (Data — printed day name, Weekly only), `log_date` (Date, required), `from_time` / `to_time` (Time), `total_hours` / `normal_shift_hours` / `overtime_hours` / `breakdown_hours` (Float, 0\-24 validated — `breakdown_hours` only meaningful on Weekly), `sign_of_crane_operator` / `sign_of_user` (Check), `work_description` (Small Text). This is what OCR populates and what the header's `working_hours`/`breakdown_hours`/`overtime_hours` are summed from.

**Equipment Log Breakdown** (`istable=1`): `from_time`, `to_time`, `duration_hours` (Float), `reason_code` (Select: Mechanical/Electrical/Hydraulic/Operator/Other), `component` (Data), `remarks` (Small Text), `evidence_attachment` (Attach). Incident\-level detail, separate from the per\-day `breakdown_hours` total in the Daily Log.

**Equipment Log Validation** (`istable=1`): `validation_type` (Select: OCR/Business/SAP), `run_id` (Data), `check_code` (Data), `status` (Select: Passed/Exception/Failed/Pending), `message` (Small Text), `input_summary` (Small Text), `source_timestamp` (Datetime), `provider_mode` (Select: Mock/Live/Azure), `is_current` (Check — only the latest run per `validation_type` is flagged current).

**Equipment Log Approval Event** (`istable=1`): `event_type` (Select: Client Decision/Maintenance Decision/SAP Validation/Sales Action/Operations Decision/Billing Outcome/System), `prior_state`, `new_state`, `decision`, `actor` (**Data**, not Link — deliberately, so the guest "Guest Client Approver" pseudo\-actor can be recorded without a User account), `actor_role`, `event_time` (Datetime), `reason_code`, `comment`, `snapshot_hash`.

## 5\. Server\-side logic

### 5\.1 `Equipment Log Sheet - Validate` (now `equipment_log_sheet.py`'s `validate()`)

This is the single source of truth for the state machine. It runs on **every** save of an Equipment Log Sheet, including the internal `d.save()` calls made by the API methods in §5.2. The pseudocode below reflects the current, restructured model (one record per Weekly/Monthly sheet, `daily_rows` child table) — see the schema note at the top of §4.

```python
if doc.period_start_date and doc.period_end_date and getdate(doc.period_end_date) < getdate(doc.period_start_date):
	frappe.throw("Period End Date cannot be before Period Start Date.")

for row in doc.daily_rows:
	for f in ["total_hours","normal_shift_hours","overtime_hours","breakdown_hours"]:
		v = frappe.utils.flt(row.get(f))
		if v < 0 or v > 24:
			frappe.throw(f"Daily Log row {row.idx}: {f} must be between 0 and 24 hours (BR-002).")

if doc.operating_site and doc.equipment and doc.sheet_template and doc.period_start_date:
	dup = frappe.db.get_value("Equipment Log Sheet", {
		"operating_site": doc.operating_site,
		"equipment": doc.equipment,
		"sheet_template": doc.sheet_template,
		"period_start_date": doc.period_start_date,
		"workflow_state": ["!=", "Void"],
		"name": ["!=", doc.name or ""],
	}, "name")
	if dup:
		frappe.throw(f"Duplicate log already exists for this site, equipment, template and period: {dup} (BR-004).")

# Header totals are computed, not entered — summed straight from the Daily Log.
doc.working_hours = sum(flt(r.total_hours) for r in doc.daily_rows)
doc.breakdown_hours = sum(flt(r.breakdown_hours) for r in doc.daily_rows)
doc.overtime_hours = sum(flt(r.overtime_hours) for r in doc.daily_rows)
doc.idle_hours = 0     # neither real template tracks this distinction — always 0
doc.standby_hours = 0  # same

if not doc.is_new() and doc.client_approval_status == "Approved" and not doc.flags.get("skip_material_check") and doc.approved_content_hash:
	# compute_content_hash() hashes operating_site/equipment/sheet_template/
	# period_start_date/period_end_date + every daily_rows row's content.
	# approved_content_hash was captured at the moment the client approved
	# (record_log_sheet_client_decision) — comparing against a fixed
	# baseline, rather than re-querying the DB's "before" scalar values,
	# is what lets this catch an edit INSIDE the daily_rows child table,
	# which a simple frappe.db.get_value diff can't see.
	if doc.compute_content_hash() != doc.approved_content_hash:
		doc.client_approval_status = "Not Requested"
		doc.client_token_hash = None
		doc.client_token_expiry = None
		doc.client_snapshot_hash = None
		doc.approved_content_hash = None
		doc.append("approval_events", {
			"event_type": "System",
			"prior_state": doc.workflow_state,
			"new_state": "Ready for Client",
			"decision": "Revoked",
			"actor": frappe.session.user,
			"actor_role": "System",
			"event_time": frappe.utils.now_datetime(),
			"comment": "Header fields or Daily Log rows changed after client approval. Approval revoked (BR-007).",
		})

if doc.workflow_state != "Void":
	if doc.billing_status == "Closed":
		doc.workflow_state = "Closed"
	elif doc.client_approval_status != "Approved":
		if doc.client_approval_status == "Rejected":
			doc.workflow_state = "Operator Rework"
		elif doc.client_approval_status == "Pending":
			doc.workflow_state = "Client Approval Pending"
		elif doc.client_approval_status == "Expired":
			doc.workflow_state = "Ready for Client"
		# else "Not Requested": leave workflow_state exactly as the caller set it
		#   (Draft / AI Review / Ready for Client are driven by run_log_sheet_ocr /
		#   the operator directly, not recomputed here)
	elif doc.maintenance_status == "Rejected":
		doc.workflow_state = "Operator Rework"
	elif doc.sap_validation_status == "Exception":
		doc.workflow_state = "Sales Action Pending"
	elif doc.operations_status == "Returned":
		doc.workflow_state = "Operator Rework"
	elif doc.maintenance_status == "Approved" and doc.sap_validation_status == "Passed":
		doc.workflow_state = "Billing Ready" if doc.operations_status == "Approved" else "Operations Review"
	else:
		doc.workflow_state = "Parallel Validation"

doc.state_entered_on = frappe.utils.now_datetime()

owner_map = {
	"Draft": "Log Sheet Operator", "AI Review": "Log Sheet Operator",
	"Ready for Client": "Log Sheet Operator", "Client Approval Pending": "Guest Client Approver",
	"Parallel Validation": "Maintenance + SAP", "Operator Rework": "Log Sheet Operator",
	"Sales Action Pending": "Log Sheet Sales Resolver", "Operations Review": "Log Sheet Operations Approver",
	"Billing Ready": "Log Sheet Billing User", "Closed": "Log Sheet Billing User", "Void": "Log Sheet Manager",
}
doc.current_owner_role = owner_map.get(doc.workflow_state, "")
```

**Design notes:**

- **`workflow_state` is always derived, never hand\-set** by an API method except for the three states that precede client approval (Draft / AI Review / Ready for Client) and the terminal `Closed`/`Void` states — everything from "Client Approval Pending" onward is recomputed from the four gate\-status fields on every save. This means a bug that directly sets `workflow_state` incorrectly self\-heals on the next save; it also means **the priority order of the `if/elif` chain above *is* the business rule** — read it top\-to\-bottom as the precedence: Closed beats everything, then client approval, then Maintenance rejection, then SAP exception, then Operations return, then the "both gates clear" happy path, else Parallel Validation.
- **BR\-007** (material\-field edit after approval revokes it) was originally a two\-field\-group DB diff (`numeric_fields` via `frappe.utils.flt()`, `text_fields` via `str()` — see §9 for why those needed separate comparison). After the restructuring it compares a **hash captured at approval time** (`approved_content_hash`) against a hash recomputed from the document's current state on every later save, because the old DB\-diff approach has no clean way to detect an edit inside a child table (`daily_rows`) — see `compute_content_hash()` in `equipment_log_sheet.py`.

### 5\.2 Whitelisted API methods (Server Script, type \= API)

All nine are `@frappe.whitelist()`\-equivalent Server Scripts (`script_type = "API"`), invoked at `/api/method/<api_method>`. Two are `allow_guest = 1` (the only ones reachable without a logged\-in session); the rest re\-implement Frappe's role check manually via a `user_roles()` helper (`frappe.get_roles()` is not available in the sandbox — see §9) and `frappe.throw(..., frappe.PermissionError)` on failure. **All nine are POST** except `get_log_sheet_approval_snapshot`, which is read\-only and safe as GET (see the warning below the table).

| Method | Access | Args |
| --- | --- | --- |
| `run_log_sheet_ocr` | Operator, Manager, Sys Mgr | `name` |
| `generate_log_sheet_client_link` | Operator, Manager, Sys Mgr | `name` |
| `get_log_sheet_approval_snapshot` | **Guest (public), GET** | `token` |
| `record_log_sheet_client_decision` | **Guest (public)** | `token`, `decision` (Approve/Reject), `comment` |
| `record_log_sheet_maintenance_decision` | Maintenance Approver, Manager, Sys Mgr | `name`, `decision` (Approved/Rejected), `reason_code`, `comment` |
| `run_log_sheet_sap_validation` | Operations Approver, Sales Resolver, Manager, Sys Mgr | `name` |
| `request_log_sheet_sap_revalidation` | Sales Resolver, Manager, Sys Mgr | `name`, `comment` (required) |
| `record_log_sheet_operations_decision` | Operations Approver, Manager, Sys Mgr | `name`, `decision` (Approved/Returned), `comment` |
| `record_log_sheet_billing_outcome` | Billing User, Manager, Sys Mgr | `name`, `action` (Close/Hold), `sap_document_reference`, `hold_reason`, `manager_override_reason` |

**What each one does:**

- **`run_log_sheet_ocr`** — dispatches to Mock or Google Vision API (per Settings), populates `daily_rows` (one row per day the scan shows — replacing whatever was there from a previous run), logs a per\-row validation entry, sets `ocr_status` and `workflow_state = AI Review`. The header utilization totals are *not* set here directly — they're recomputed from `daily_rows` by `validate()` on the `doc.save()` this method makes.
- **`generate_log_sheet_client_link`** — issues an opaque token, hashes and stores it, snapshots the figures being approved, sets `workflow_state = Client Approval Pending`.
- **`get_log_sheet_approval_snapshot`** — returns the figures for the public guest page, or the generic error.
- **`record_log_sheet_client_decision`** — records the client's Approve/Reject; on Approve it opens both parallel gates at once (`maintenance_status` and `sap_validation_status` → `Pending`).
- **`record_log_sheet_maintenance_decision`** — sets the Maintenance gate.
- **`run_log_sheet_sap_validation`** — runs the five Mock SAP checks per `Log Sheet Automation Settings.mock_sap_scenario`, sets the SAP gate.
- **`request_log_sheet_sap_revalidation`** — logs the corrective action taken by Sales; the caller then re\-invokes `run_log_sheet_sap_validation`.
- **`record_log_sheet_operations_decision`** — **BR\-010**\: re\-checks all three gates server\-side before allowing Approve; calculates `billable_hours` from the Billing Rule and sets `workflow_state = Billing Ready`.
- **`record_log_sheet_billing_outcome`** — closes (`workflow_state = Closed`) or holds billing; **BR\-011**.

> **Mutating calls must be POST.** GET requests against these endpoints were observed to return `{"ok": true}` without persisting the write (Frappe does not reliably auto\-commit a GET request) — confirmed during testing on `record_log_sheet_client_decision`. Only the read\-only `get_log_sheet_approval_snapshot` is safe as GET.

Full source for the two guest\-facing endpoints and the three internal decision endpoints (the OCR/link/SAP scripts are reproduced inline where relevant above and in §6):

```python
# record_log_sheet_client_decision — allow_guest = 1
token = frappe.form_dict.get("token")
decision = frappe.form_dict.get("decision")
comment = frappe.form_dict.get("comment")
generic_error = {"ok": False, "error": "This approval link is invalid or has expired."}

if not token or decision not in ("Approve", "Reject"):
	frappe.response["message"] = generic_error
else:
	token_hash = frappe.utils.sha256_hash(token)
	rows = frappe.get_all("Equipment Log Sheet",
		filters={"client_token_hash": token_hash, "client_approval_status": "Pending"},
		fields=["name","client_token_expiry"], limit_page_length=1)
	if not rows:
		frappe.response["message"] = generic_error
	else:
		row = rows[0]
		if frappe.utils.now_datetime() > frappe.utils.get_datetime(row.client_token_expiry):
			frappe.response["message"] = generic_error
		elif decision == "Reject" and not comment:
			frappe.response["message"] = {"ok": False, "error": "A comment is required to reject."}
		else:
			d = frappe.get_doc("Equipment Log Sheet", row.name)
			d.client_decision_on = frappe.utils.now_datetime()
			d.client_comment = comment
			if decision == "Approve":
				d.client_approval_status = "Approved"
				d.maintenance_status = "Pending"
				d.sap_validation_status = "Pending"
			else:
				d.client_approval_status = "Rejected"
				d.client_token_hash = None
				d.client_token_expiry = None
			d.append("approval_events", { ... })   # Client Decision event, actor="Guest Client Approver"
			d.flags.ignore_permissions = True
			d.save(ignore_permissions=True)
			frappe.response["message"] = {"ok": True, "decision": decision, "log": d.name}
```

```python
# record_log_sheet_operations_decision — the billing calculation (BR-010/BR-011 core)
if decision == "Approved":
	if d.client_approval_status != "Approved" or d.maintenance_status != "Approved" or d.sap_validation_status != "Passed":
		frappe.throw("Cannot approve: client, Maintenance and SAP gates must all be clear (BR-010).")
	rule = frappe.get_doc("Log Sheet Billing Rule", d.billing_rule)
	gross_hours = frappe.utils.flt(d.working_hours)
	if rule.bill_idle_hours:     gross_hours += frappe.utils.flt(d.idle_hours)
	if rule.bill_standby_hours:  gross_hours += frappe.utils.flt(d.standby_hours)
	if rule.bill_breakdown_hours: gross_hours += frappe.utils.flt(d.breakdown_hours)
	minimum = frappe.utils.flt(rule.minimum_daily_hours)
	after_minimum = gross_hours if gross_hours > minimum else minimum
	increment = frappe.utils.flt(rule.rounding_increment) or 0.5
	units = after_minimum / increment
	whole_units = units // 1
	if rule.rounding_method == "Up":
		if units > whole_units: whole_units += 1
	elif rule.rounding_method == "Down":
		pass
	else:  # Nearest
		if (units - whole_units) >= 0.5: whole_units += 1
	billable = whole_units * increment
	d.billable_hours = billable
	d.calculation_trace = json.dumps({...}, indent=2)   # full trace: every input + intermediate value
	d.operations_status = "Approved"
	d.billing_status = "Ready"
```

```python
# record_log_sheet_billing_outcome — BR-011
if action == "Hold":
	if not hold_reason: frappe.throw("A hold reason is mandatory (BR-011).")
	d.billing_status = "Held"; d.billing_hold_reason = hold_reason
else:  # Close
	is_manager = bool(user_roles() & {"Log Sheet Manager","System Manager"})
	if not sap_document_reference and not (is_manager and override_reason):
		frappe.throw("A manual SAP document reference is required, or a Manager override reason (BR-011).")
	d.sap_document_reference = sap_document_reference
	d.billing_status = "Closed"; d.closed_on = frappe.utils.now_datetime(); d.workflow_state = "Closed"
```

### 5\.3 Business rules implemented (as coded, with source references)

| Rule | Enforced in | Behaviour |
| --- | --- | --- |
| **BR\-001** | `generate_log_sheet_client_link` | Blocks link generation if `operating_site`, `equipment`, `sheet_template`, `period_start_date`, or `operator_user` is missing. |
| **BR\-002** | `Equipment Log Sheet - Validate` | Every hour field on every Daily Log row must be 0–24. |
| **BR\-004** | `Equipment Log Sheet - Validate` | One log per (site, equipment, sheet template, period start date) combination among non\-Void records. |
| **BR\-005** | `generate_log_sheet_client_link` | An active Commercial Mapping valid as of `period_start_date` must exist; its SAP/UOM/rate/billing\-rule fields are pulled onto the sheet. |
| **BR\-006** | `generate_log_sheet_client_link` | If OCR ran and flagged low\-confidence rows, `ocr_review_complete` must be ticked before a client link can be generated. |
| **BR\-007** | `Equipment Log Sheet - Validate` | Editing a header identity field or any Daily Log row after `client_approval_status = Approved` resets it to `Not Requested` and clears the token/snapshot/approved\-content hash — approval must be re\-obtained. |
| **BR\-008** | `record_log_sheet_maintenance_decision` | Reason code \+ comment mandatory on Maintenance rejection. |
| **BR\-010** | `record_log_sheet_operations_decision` | Server\-side re\-check that all three gates are clear before allowing Operations approval — cannot be bypassed by manipulating client\-side button visibility. |
| **BR\-011** | `record_log_sheet_billing_outcome` | Closing billing requires a SAP document reference, or a Manager/System Manager override reason; holding requires a reason. |
| **BR\-012** | `get_log_sheet_approval_snapshot`, `record_log_sheet_client_decision` | Every failure mode on the guest endpoints (unknown token, wrong status, expired) returns the **same generic error string** — no information disclosure about which part of the lookup failed. |

## 6\. Guest client\-approval security model

The public approval flow is designed so a client can act on a log sheet with **no ERPNext account** while still being resistant to link\-guessing and after\-the\-fact tampering:

1. `generate_log_sheet_client_link` mints a 32\-character random token (`frappe.utils.generate_hash(length=32)`) — **the token itself is only ever returned once, in the API response**, never persisted.
2. Only `sha256_hash(token)` is stored on the document (`client_token_hash`), so a database read of Equipment Log Sheet never exposes a usable token.
3. A canonical JSON snapshot of the figures the client is being asked to approve is hashed (`client_snapshot_hash`) and stamped onto every audit\-trail event from that decision — this is what would let you prove, after the fact, exactly what the client saw.
4. `client_token_expiry` (default from `Log Sheet Automation Settings.client_token_ttl_hours`) bounds the link's lifetime.
5. Both public endpoints are `allow_guest = 1` and reachable with **no session cookie / no CSRF token** — confirmed working with `fetch(url, {credentials: "omit"})`. Frappe only enforces CSRF for authenticated sessions, so this is correct behaviour for a true anonymous request, not a bypass.
6. Every failure path returns the identical `{"ok": false, "error": "This approval link is invalid or has expired."}` regardless of cause (BR\-012) — deliberately conflating "wrong token", "already decided", and "expired" so an attacker can't distinguish a guessed\-wrong token from a real\-but\-expired one.
7. On Approve, both `maintenance_status` and `sap_validation_status` flip from `Not Started`/blank to `Pending` in the same write that records the decision — this is what opens the two parallel gates.

## 7\. Client Script — `Equipment Log Sheet Actions`

Single Client Script, `dt = Equipment Log Sheet`, `view = Form`. Renders role\- and state\-conditional toolbar buttons via `frm.add_custom_button`, each POSTing to its matching API method (via `frappe.call`) and then `frm.reload_doc()`. Buttons present, gated on `frappe.user_roles` and the current `workflow_state`/gate\-status fields:

`Run OCR` · `Generate Client Approval Link` · `Record Maintenance Decision` · `Run SAP Validation` · `Request SAP Revalidation` · `Approve (Operations)` · `Return (Operations)` · `Close Billing` · `Hold Billing`

A `frm.dashboard.add_indicator` call shows the current `workflow_state` as a coloured status pill at the top of the form (green for Closed, etc.).

> Role gating here is **UX only** — a button being hidden does not by itself protect the transition. Every one of these actions is independently re\-guarded server\-side inside its own Server Script (§5.2), which is where the real access control lives.

## 8\. Workspace, Number Cards & Print Format

### 8\.1 Workspace `Log Sheet Automation`

`Workspace.autoname = field:label` (not `title` — easy to miss). Content is a JSON block array (`header`/`paragraph`/`shortcut`/`number_card` block types) **plus** a matching `number_cards` child table (`Workspace Number Card`\: `number_card_name`, `label`). Both must be populated for a Number Card block to render — the content\-block JSON alone (`type:"number_card"`, `data.number_card_name`) is **not sufficient**; Frappe v15 also expects a corresponding row in the Workspace's own `number_cards` table, mirroring how `shortcuts` blocks pair with the `shortcuts` child table. Populating only the JSON silently renders nothing (no error) — this was the one non\-obvious piece of this build; see §9.

Five **Number Card** documents (`document_type = Equipment Log Sheet`, `function = Count`, `is_public = 1`), four of them wired into the workspace dashboard:

| Card | Filter |
| --- | --- |
| LSA \- Total Active Logs | `workflow_state not in [Closed, Void]` |
| LSA \- Sales Exceptions | `sap_validation_status = Exception` |
| LSA \- Billing Ready | `workflow_state = Billing Ready` |
| LSA \- Operator Rework | `workflow_state = Operator Rework` |
| LSA \- Closed (Demo) | `workflow_state = Closed` (defined, not placed on the workspace) |

Shortcuts cover: New Log Sheet, All Log Sheets, My Drafts, Operator Rework, Maintenance Pending, Sales Exceptions, Operations Review, Billing Ready, plus one shortcut per master DocType and Settings.

### 8\.2 Print Format `Log Sheet Approval Summary`

`print_format_for = DocType`, `doc_type = Equipment Log Sheet`, `custom_format = 1`, `print_format_type = Jinja`. Renders: identity, utilization, client decision, Maintenance decision, the **latest** SAP validation run only (`validation_table | selectattr("validation_type","equalto","SAP") | selectattr("run_id","equalto", <latest run_id>)`), Operations decision, and the billing calculation. **Deliberately excludes** `client_token_hash`, `client_snapshot_hash`, `calculation_trace` raw JSON, and OCR/SAP `input_summary` payloads — per the SRS requirement to keep internal security/integration artifacts off any client\-facing printout.

## 9\. RestrictedPython sandbox — constraints discovered

Frappe Server Scripts execute inside `safe_exec` (RestrictedPython), which is materially more restrictive than a normal Frappe app controller. These are the concrete failures hit while building this module, and the working replacement for each — worth keeping for whoever edits these scripts next:

| Don't use | Fails with | Use instead |
| --- | --- | --- |
| `import x` / `from x import y` | `ImportError: __import__ not found` | Only pre\-bound globals (`frappe`, `json`, etc.) are available — no import statements at all. |
| `"...{0}...".format(x)` | `AttributeError: 'format' is an unsafe attribute` | f\-strings, or `+` concatenation. |
| `frappe.get_roles()` / `frappe.only_for()` | `AttributeError` | `frappe.get_all("Has Role", filters={"parent": frappe.session.user, "parenttype": "User"}, fields=["role"])` → build a set manually. |
| `frappe.get_single(dt)` | `AttributeError` | `frappe.get_doc(doctype, doctype)` (Single docs are name\-keyed by their own doctype). |
| `frappe.as_json(x)` | `AttributeError` | The global `json` module is available directly: `json.dumps(x)`. |
| `frappe.generate_hash()` / `frappe.utils.random_string()` / `frappe.utils.md5()` | Returns `None` → `'NoneType' object is not callable` on next use (no exception at the call site itself) | `frappe.utils.generate_hash(length=N)` for tokens, `frappe.utils.sha256_hash(str)` for hashing. |
| List/dict comprehensions referencing an outer\-scope variable inside a nested function | `NameError: name 'x' is not defined` (scoping bug specific to `exec()` globals/locals split under RestrictedPython) | Rewrite as an explicit top\-level `for` loop instead of a comprehension. |
| `doc.get_doc_before_save()` | Unreliable/misbehaves in\-sandbox | `frappe.db.get_value(doctype, name, fields, as_dict=True)` to fetch the pre\-save DB row explicitly. |
| Comparing a DB\-fetched Decimal\-like value to a plain float with `==` | False "changed" positives (e.g. `"1.000000" != "1.0"` as strings) | Compare numeric fields with `frappe.utils.flt(a) != frappe.utils.flt(b)`, and only compare true text fields with `str(a or "") != str(b or "")` (used in the BR\-007 check in §5.1). |
| A GET request to a mutating whitelisted method | Returns `{"ok": true}` but the write is not committed | Always call mutating endpoints with `POST` (confirmed: guest decision endpoint tested via GET silently no\-ops). |

Other build gotchas worth recording:

- **Asset creation** on this site requires a real `Location` document (not a free\-text string) and is entangled with the org's live accounting/asset ledger — demo Assets were created against company **"LMC Test"**, left **unsubmitted** (`docstatus=0`), with `gross_purchase_amount=1` and `calculate_depreciation=0` to avoid touching production financials.
- **India Compliance** app on this site makes `gst_hsn_code` mandatory on new Items.
- **Workspace autoname** is `field:label`, not `field:title` — inserting a Workspace doc without `label` fails with a generic "Name is required" error that doesn't point at the missing field.
- Server Script API method **argument names come from `frappe.form_dict`**, i.e. whatever key the caller sends — there is no independent "parameter" declaration to check against, so a wrong key name silently resolves to `None` inside the script rather than erroring at the boundary (e.g. sending `log_sheet` instead of `name` produced `"Equipment Log Sheet None not found"`, not an argument error).

## 10\. Deployment notes & promotion path

This build is **live configuration on the production site**, not a package that can be installed on another instance by itself. To promote it to a proper, portable, version\-controlled app:

1. `bench new-app log_sheet_automation`, then either `bench export-fixtures` the DocTypes/Roles/Workspace/Print Format as fixtures, or recreate them as first\-class `.json` DocType definitions in the app (cleaner long\-term — fixtures are fragile across versions).
2. Move each Server Script's body into a proper controller (`hooks.py` `doc_events` for the Before Save validation; `@frappe.whitelist()` Python functions in an `api.py` for the nine methods) — this also removes every constraint in §9, since normal app code runs outside the RestrictedPython sandbox.
3. Move the Client Script into the DocType's client\-side `.js` bundle.
4. Swap `Log Sheet Automation Settings.ocr_provider_mode` / `sap_provider_mode` from Mock to their real integrations behind the same settings surface — no workflow logic changes required, by design. **Done**: OCR now calls Google Vision API (not Azure — Azure was evaluated, then dropped once the real paper log sheets showed a multi\-day table shape that would need a different custom\-model schema entirely); SAP now calls a live, configurable REST endpoint.
5. Wire actual email/SMS delivery of the client approval link. **Done** — Email via the site's Email Account, SMS via Twilio, both dispatched from `notifications.py`.
6. Add automated tests for the state\-machine transitions, the BR\-007 content\-hash comparison, and the Daily Log aggregate computation before this carries real invoices.
7. **Done (2026\-10):** restructured Equipment Log Sheet from one\-record\-per\-day to one\-record\-per\-Weekly/Monthly\-sheet with a Daily Log child table, after reviewing the real Sanghvi Movers paper templates — see the schema note at the top of §4 and `equipment_log_sheet.py`'s module docstring.
