# Log Sheet Automation

A Frappe/ERPNext v15 app that turns Sanghvi Movers' Weekly or Monthly crane log sheet into a single record that moves itself through capture, OCR, client approval, parallel Maintenance + SAP validation, Operations sign-off, and Billing — with a full audit trail at every step.

**Schema note (2026-10):** after reviewing the real Sanghvi Movers paper log sheets (a Weekly Crane Log Sheet and a Monthly Crane Logsheet template, neither of which is a single-day form), **Equipment Log Sheet** was restructured from "one record per single day" to **one record per Weekly/Monthly sheet, with a Daily Log child-table row per day** (`Equipment Log Sheet Day`). `working_hours` / `breakdown_hours` / `overtime_hours` are now computed by summing the Daily Log rows on every save; `idle_hours` / `standby_hours` are kept on the schema (so nothing downstream needed to change) but always stay 0, since neither real template tracks that distinction.

This app is the git-tracked, installable version of a workflow that was first prototyped directly on a live ERPNext Desk UI (Custom DocTypes, Server Scripts, a Client Script, a Workspace, a Print Format) as a same-day MVP demo. Every DocType, business rule, API method, and UI behaviour here is a faithful, cleaned-up port of that working prototype — see [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) for the full mapping from "Desk Server Script" to "app controller code", including the RestrictedPython sandbox workarounds that no longer apply now that this runs as ordinary app code.

## What it does

1. An operator creates an **Equipment Log Sheet** for a site + equipment and attaches the scan (photo, image, or a scanned PDF — a PDF with several pages is treated as one log sheet per page).
2. **Run OCR** reads the scan with **Google Vision API** and fills in the sheet: Weekly/Monthly template, month and period, the Daily Log (one row per day) and the handwritten header as read. Page 1 fills the sheet OCR was run from; every further page of the PDF becomes its own new Equipment Log Sheet. Anything read with low confidence is listed under Validation Results and keeps the sheet in AI Review. The header's hour totals are then computed automatically from the Daily Log rows.
3. A one-click, no-login link lets the client approve or reject the sheet — the link is emailed and/or texted to the **Client Recipient** automatically (Email via the site's own Email Account, SMS via Twilio), in addition to being shown in the UI.
4. On approval, two gates open **in parallel**: Maintenance and SAP validation — SAP validation POSTs the log sheet's actual data (hours, every Daily Log row, breakdown lines, billable hours, references) to your real SAP-fronting REST endpoint (the only SAP mode this deployment offers). Both gates must clear before the sheet moves on.
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
| Equipment Log Sheet Day | Child table |
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

After install, open **Log Sheet Automation Settings** and fill in the Google Vision and SAP fields below before running a demo — this deployment no longer has a no-credentials Mock fallback for either (see below for why).

### Upgrading a site that ran the original Desk prototype

The first version of this workflow lived on the site itself as one Client Script (**Equipment Log Sheet Actions**), ten Server Scripts (`run_log_sheet_ocr`, `generate_log_sheet_client_link`, `Equipment Log Sheet - Validate`, ...) and a Web Page (**log-sheet-approval**). If they are still enabled they shadow this app: the form shows a second **Actions → Run OCR** button that runs the old *mock* OCR (fake data, no Google call), and the old Web Page is served instead of this app's approval page. Disable the Client Script and the Server Scripts and unpublish the Web Page (nothing needs deleting) so only the app's code runs.

## Integrations & notifications

`OCR Provider Mode` is locked to **Google Vision API** and `SAP Provider Mode` is locked to **Live** — Mock is no longer offered as a choice in Settings. The Mock code path still exists in `integrations/ocr.py` and `integrations/sap.py` (harmless, unreachable from the UI) in case you ever want an offline fallback again, but every OCR run and every SAP validation run now goes to the real service. (The earlier Azure Document Intelligence OCR adapter and the Document AI adapter before it have both been removed — not just locked out — because Equipment Log Sheet's restructuring to a Daily Log child table made their old four-scalar-field extraction shape obsolete; rebuilding either against the new row shape would be a fresh effort, not a flag flip.)

Both external integrations, and client notification delivery, are real working code — not stubs — but they haven't been exercised against your actual live Google Vision/SAP/Twilio credentials yet, because none were available in the environment this app was built in. Review and test each one against your real endpoint/account before a go-live.

| Concern | What happens now (Live-only) |
|---|---|
| OCR | In Settings, upload the service account key `.json` file from Google Cloud under **Google Service Account Key File** and save (the file is checked, stored encrypted, and the upload itself deleted; **Google Key Loaded** then shows which key is in use), then click **Test Google Vision Connection** there: one tiny request that proves the key, the Cloud Vision API being enabled, and billing. `Run OCR` then (1) splits the attached `Source Document` into one image per page and shrinks each to fit Vision's ~10 MB request limit (`integrations/ocr_pages.py`; needs `pypdf` + `Pillow`, both part of every bench), (2) sends each page to `images:annotate` with `DOCUMENT_TEXT_DETECTION` (`integrations/ocr.py`; needs `google-auth`), and (3) rebuilds the table from the **positions** of the words Vision returns (`integrations/ocr_layout.py`): the printed column headings give the columns, the printed day names (Weekly) or the handwriting in the DATE column (Monthly) give the rows, and each cell is parsed as a date / time / hours with tolerance for the usual handwriting misreads. Vision has no notion of a table, which is why the earlier line-by-line regular expression never matched a ruled, handwritten form. Each row gets a confidence from Vision's own word confidences plus cross-checks (hours vs From–To, Normal + OT vs Total, dates in sequence); below **OCR Low-Confidence Threshold** it is flagged for review. With **Keep OCR Word Dump** on, each sheet also gets a small private JSON attachment of every word and its position, so a misread sheet can be diagnosed and the layout logic re-tested (`tests/test_ocr_layout.py`) without another Vision call. **Status:** tuned against live Vision output for two real scans (4 Weekly + 4 Monthly pages): every sheet and row was found, about 96% of the date / time / hours cells were read correctly, and every row containing a wrong cell was flagged for review. Handwriting accuracy still depends on scan quality (the Weekly sample was only ~100 dpi), so AI Review remains a real step, not a formality. |
| SAP validation | Set **SAP Endpoint / Auth Type / Credential** in Settings. `Run SAP Validation` POSTs the log sheet's actual data — not just reference IDs — to that endpoint: site, customer, equipment, sheet template, month, period dates, all five hour-total fields, billable hours, every Daily Log row, every breakdown line, and the sales order / work order / rate references (see `_build_sap_payload()` in `sap.py` for the exact shape). It expects back `{"checks": [{"code","status","message"}]}`. This is a generic REST contract, not any specific SAP module's API — replace `_call_live_endpoint()` in `sap.py` with whatever your real SAP integration layer (OData, PI/PO, RFC middleware, BTP...) expects; nothing else in the app needs to change as long as it returns that same `checks` shape. If the endpoint is unreachable, the gate fails **closed** (routed to the Sales queue as a "Failed" check), never silently passed. |
| SAP posting | Set **SAP Posting Endpoint** in Settings. After Operations approval, **Post to SAP** sends the approved sheet with its calculated billable hours (payload: `build_posting_payload()` in `sap.py`) and expects back `{"sap_document_number": "..."}`, which is stored as the SAP Document Reference and closes the sheet. This is a **write** to SAP, which the original MVP SRS kept out of scope ("No SAP posting is permitted in this MVP"); it was added at the project lead's request. The payload carries `idempotency_key` (the log sheet number): the real SAP-side service must use it to refuse a duplicate posting, and reversal/cancellation in SAP is not handled by this app. |
| Client notification | Set **Notification Channel** (Email / SMS / Email and SMS) in Settings. Email sends via `frappe.sendmail()` using whatever Email Account is already configured on the site — no new credentials needed. SMS sends via the Twilio REST API — fill in **Twilio Account SID / Auth Token / From Number**. A delivery failure is logged to the Error Log and recorded on the log sheet's own audit trail, but never blocks or rolls back the already-saved approval link. |

All of these are dispatched from a single settings-driven switch, so the workflow logic in `api.py` and `equipment_log_sheet.py` didn't need to change to go Live-only.

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
│   ├── ocr.py                 # Google Vision calls, service-account key checks
│   ├── ocr_pages.py           # scan (image / multi-page PDF) -> one small image per page
│   ├── ocr_layout.py          # Vision word positions -> Daily Log rows + sheet header
│   └── sap.py                 # Mock / Live SAP validation adapter (fail-closed)
├── notifications.py          # client approval link delivery (Email / Twilio SMS)
├── tests/                    # plain-Python tests for ocr_pages / ocr_layout (no site needed)
├── www/log-sheet-approval.*  # the public, no-login client approval page
└── log_sheet_automation/     # the module: doctype/<name>/{.json,.py,.js}
    └── doctype/
        ├── equipment_log_sheet/        # main transaction + state machine + form buttons
        ├── equipment_log_sheet_day/    # child table — one Daily Log row per day
        ├── operating_site/
        ├── log_sheet_ocr_template/
        ├── log_sheet_billing_rule/
        ├── site_equipment_commercial_mapping/
        ├── log_sheet_automation_settings/
        ├── equipment_log_breakdown/    # child table — incident-level breakdown detail
        ├── equipment_log_validation/   # child table
        └── equipment_log_approval_event/  # child table
```

## Documentation

- [`docs/FUNCTIONAL_GUIDE.md`](docs/FUNCTIONAL_GUIDE.md) — what every DocType and field is for, and the step-by-step demo script.
- [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) — full schema, the state-machine logic, the API reference, the guest-approval security model, and deployment notes.

## License

MIT — see [`LICENSE`](LICENSE).
