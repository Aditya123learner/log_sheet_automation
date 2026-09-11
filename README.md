# Log Sheet Automation

A Frappe/ERPNext v15 app that turns Sanghvi Movers' daily crane log sheet into a single record that moves itself through capture, OCR, client approval, parallel Maintenance + SAP validation, Operations sign-off, and Billing — with a full audit trail at every step.

This app is the git-tracked, installable version of a workflow that was first prototyped directly on a live ERPNext Desk UI (Custom DocTypes, Server Scripts, a Client Script, a Workspace, a Print Format) as a same-day MVP demo. Every DocType, business rule, API method, and UI behaviour here is a faithful, cleaned-up port of that working prototype — see [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) for the full mapping from "Desk Server Script" to "app controller code", including the RestrictedPython sandbox workarounds that no longer apply now that this runs as ordinary app code.

## What it does

1. An operator creates a daily **Equipment Log Sheet** for a site + equipment + shift.
2. OCR extracts the hours and flags low-confidence fields for review — **Mock** mode (canned values, no credentials) or **Azure** mode (a real Azure AI Document Intelligence call), selected in Settings.
3. A one-click, no-login link lets the client approve or reject the sheet — the link is emailed and/or texted to the **Client Recipient** automatically (Email via the site's own Email Account, SMS via Twilio), in addition to being shown in the UI.
4. On approval, two gates open **in parallel**: Maintenance and SAP validation (**Mock** canned checks or a **Live** call to a real SAP-fronting REST endpoint, selected in Settings). Both must clear before the sheet moves on.
5. A SAP exception — or a Live SAP endpoint that couldn't be reached at all — routes to a Sales queue for correction and revalidation — the sheet is never stuck, and a connectivity failure never silently passes the gate.
6. Operations gives final sign-off, and **billable hours are calculated automatically** from a configurable billing rule.
7. Billing closes the sheet with a SAP document reference.

Every decision is timestamped in an immutable audit trail (`Equipment Log Approval Event`), and the guest approval link uses a hash-only token model so a database read never exposes a usable link (see the security section of the technical design doc).

## DocTypes

| DocType | Type |
|---|---|
| Operating Site | Master |
| Log Sheet OCR Template | Master |
| Log Sheet Billing Rule | Master |
| Site Equipment Commercial Mapping | Master |
| Log Sheet Automation Settings | Single |
| Equipment Log Breakdown | Child table |
| Equipment Log Validation | Child table |
| Equipment Log Approval Event | Child table |
| **Equipment Log Sheet** | Main transaction |

Plus 7 custom roles (`Log Sheet Operator`, `Maintenance Approver`, `Sales Resolver`, `Operations Approver`, `Billing User`, `Manager`, `Auditor`), a Workspace with live KPI Number Cards, and a client-facing Print Format that deliberately excludes internal security fields.

## Installation

```bash
# from your bench directory
bench get-app log_sheet_automation https://github.com/Aditya123learner/log_sheet_automation.git
bench --site <your-site> install-app log_sheet_automation
bench --site <your-site> migrate
```

Fixtures (the 7 roles, the Workspace, the Print Format, and the 5 Number Cards) are applied automatically on install/migrate via `hooks.py`'s `fixtures` list.

After install, open **Log Sheet Automation Settings** and confirm both integrations are set to **Mock** mode before running a demo — no external OCR/SAP credentials are required in that mode.

## Integrations & notifications

Both external integrations, and client notification delivery, are real working code — not stubs — but only the Mock paths have actually been exercised, because no live Azure/SAP/Twilio credentials were available in the environment this app was built in. Review and test each one against your real endpoint/account before a go-live.

| Concern | Mock (default) | Live |
|---|---|---|
| OCR | `log_sheet_automation/integrations/ocr.py` returns fixed canned values | Two live options, both dispatched from the same `OCR Provider Mode` setting: **Azure** — fill in **Azure Endpoint / API Key / Model ID / API Version**; calls the Azure AI Document Intelligence `analyze` → poll → parse flow. **Google Document AI** — fill in **Google Cloud Project ID / Location / Processor ID / Service Account Key (JSON)**; calls a Document AI processor's `:process` REST endpoint, authenticating via the pasted service-account key (needs the `google-auth` package — see `pyproject.toml`). Either way, your OCR model/processor must return numeric fields named exactly `working_hours`, `idle_hours`, `standby_hours`, `breakdown_hours` (or edit `FIELD_NAMES` in `ocr.py` to match your model's actual schema). |
| SAP validation | `log_sheet_automation/integrations/sap.py` returns the 5 canned checks (with `mock_sap_scenario` to force an exception for a demo) | Set **SAP Provider Mode** = Live and fill in **SAP Endpoint / Auth Type / Credential**. POSTs a JSON payload and expects back `{"checks": [{"code","status","message"}]}`. This is a generic REST contract, not any specific SAP module's API — replace `_call_live_endpoint()` in `sap.py` with whatever your real SAP integration layer (OData, PI/PO, RFC middleware, BTP...) expects; nothing else in the app needs to change as long as it returns that same `checks` shape. If the endpoint is unreachable, the gate fails **closed** (routed to the Sales queue as a "Failed" check), never silently passed. |
| Client notification | N/A — the approval link is always shown in the UI regardless of channel | Set **Notification Channel** (Email / SMS / Email and SMS) in Settings. Email sends via `frappe.sendmail()` using whatever Email Account is already configured on the site — no new credentials needed. SMS sends via the Twilio REST API — fill in **Twilio Account SID / Auth Token / From Number**. A delivery failure is logged to the Error Log and recorded on the log sheet's own audit trail, but never blocks or rolls back the already-saved approval link. |

All of these are dispatched from a single settings-driven switch, so the workflow logic in `api.py` and `equipment_log_sheet.py` is identical regardless of which mode is active.

**On the Google service account key specifically:** it is only ever read at runtime from the `Google Service Account Key (JSON)` field on `Log Sheet Automation Settings` — a Password-type field, stored encrypted in this site's own database via Frappe's `get_password()`. It is never hardcoded in this repo or written to any file that goes into source control. Paste your key into that field from the Desk UI *after* the app is installed on your site. If a key you're using was ever pasted into a chat, ticket, doc, or any other non-secret-manager location, treat it as compromised — rotate/delete it in Google Cloud Console (IAM & Admin → Service Accounts → Keys) and issue a fresh one, regardless of whether the old one was actually used for anything.

## Configuring for a demo

1. Create at least one **Operating Site**, one **Log Sheet OCR Template**, one **Log Sheet Billing Rule**, and one **Site Equipment Commercial Mapping** per equipment/site combination you want to demo.
2. Assign the appropriate custom role(s) to your demo user(s) (System Manager can act in every role's place for a one-person demo).
3. Open the **Log Sheet Automation** workspace and click **New Log Sheet** to start.

The full click-by-click demo script (create → OCR → client approval → Maintenance + SAP → Operations → Billing close) is in [`docs/FUNCTIONAL_GUIDE.md`](docs/FUNCTIONAL_GUIDE.md).

## Repository layout

```
log_sheet_automation/
├── hooks.py                  # app wiring: fixtures, website route, module
├── modules.txt
├── api.py                    # the 9 whitelisted controller methods
├── config/desktop.py         # module icon/registration
├── fixtures/                 # Role, Workspace, Print Format, Number Card exports
├── integrations/
│   ├── ocr.py                 # Mock / Azure Document Intelligence OCR adapter
│   └── sap.py                 # Mock / Live SAP validation adapter (fail-closed)
├── notifications.py          # client approval link delivery (Email / Twilio SMS)
├── www/log-sheet-approval.*  # the public, no-login client approval page
└── log_sheet_automation/     # the module: doctype/<name>/{.json,.py,.js}
    └── doctype/
        ├── equipment_log_sheet/        # main transaction + state machine + form buttons
        ├── operating_site/
        ├── log_sheet_ocr_template/
        ├── log_sheet_billing_rule/
        ├── site_equipment_commercial_mapping/
        ├── log_sheet_automation_settings/
        ├── equipment_log_breakdown/    # child table
        ├── equipment_log_validation/   # child table
        └── equipment_log_approval_event/  # child table
```

## Documentation

- [`docs/FUNCTIONAL_GUIDE.md`](docs/FUNCTIONAL_GUIDE.md) — what every DocType and field is for, and the step-by-step demo script.
- [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) — full schema, the state-machine logic, the API reference, the guest-approval security model, and deployment notes.

## License

MIT — see [`LICENSE`](LICENSE).
