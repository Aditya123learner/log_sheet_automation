# Log Sheet Automation — Functional Specification & Demo Guide

**Client:** Sanghvi Movers &nbsp;|&nbsp; **Platform:** ERPNext (lmcpl.erpnext.com) &nbsp;|&nbsp; **Module:** Log Sheet Automation &nbsp;|&nbsp; **Status:** MVP demo build

Table of Contents

## 1\. What this system does

Sanghvi Movers' crane operators fill out a paper log sheet covering a **Weekly** or **Monthly** period, recording day by day how many hours a crane worked, its overtime, and (on the Weekly template) breakdown time. Today that paper sheet has to be manually typed up, emailed to the client for sign\-off, checked against maintenance and SAP records by hand, and only then handed to billing. It is slow and error\-prone, and mistakes are usually only caught after the invoice has already gone out.

> **Note (2026\-10):** this guide was originally written around a one\-record\-per\-single\-day prototype. After reviewing the real Sanghvi Movers paper templates, the app was restructured so one **Equipment Log Sheet** record covers a whole Weekly or Monthly sheet, with a **Daily Log** row per day — closer to how the paper actually works. Sections below have been updated to match; the overall workflow (OCR → client approval → Maintenance/SAP → Operations → Billing) is unchanged.

The **Log Sheet Automation** app turns that paper process into a single ERPNext record — the **Equipment Log Sheet** — that moves itself through every stage automatically:

1. An operator (or, in the full production version, a scan of the paper sheet) captures the day's hours.
2. The system reads the sheet (Mock OCR in this demo, a real OCR provider later) and flags anything it is not confident about.
3. A one\-click, no\-login link is emailed/texted to the client so they can approve or reject the sheet themselves.
4. Once the client approves, **two independent checks run at the same time**\: Maintenance confirms the equipment was fit for work, and SAP is checked for a valid sales order, quantity and rate. Both must pass before the sheet can move on.
5. If SAP finds a problem, it goes to a Sales queue to get sorted out — the log sheet is not stuck, it just waits there.
6. Operations gives a final sign\-off.
7. Billing calculates the billable hours automatically from the rules you configure, and closes the sheet with a reference to the SAP invoice/document.

Every step is timestamped and logged, so at any point you can see exactly who did what, when, and why a sheet is sitting where it is.

## 2\. Who uses it — roles

Seven roles control who can see and do what. A user can hold more than one role (e.g. a small back office might have one person as both Operations Approver and Billing User).

| Role | What they do in this system |
| --- | --- |
| **Log Sheet Operator** | Creates the daily log sheet, enters hours, runs OCR, generates the client approval link. Owns the sheet while it's in Draft / AI Review / Operator Rework. |
| **Log Sheet Maintenance Approver** | Reviews equipment condition and approves/rejects the Maintenance gate. |
| **Log Sheet Sales Resolver** | Works the Sales exception queue when SAP validation raises a problem (expired sales order, insufficient quantity, etc.) and requests re\-validation once it's fixed. |
| **Log Sheet Operations Approver** | Gives the final operational sign\-off before a sheet can go to Billing; can also return a sheet for rework. |
| **Log Sheet Billing User** | Closes billing with a SAP document reference, or places a sheet on billing hold. |
| **Log Sheet Manager** | Full read/write across the module; can act in place of any of the above roles; manages masters and configuration. |
| **Log Sheet Auditor** | Read\-only access everywhere — for compliance/audit review of the full trail. |

Client approval is done by an outside guest through a public link — the client does **not** need an ERPNext user account or login at all.

## 3\. Setting up the masters (configuration, done once)

Before the first log sheet is created, a few master records need to exist. These live under **Masters & Configuration** on the Log Sheet Automation workspace.

### 3\.1 Operating Site

One record per crane yard / site the equipment operates from.

| Field | Purpose |
| --- | --- |
| Site Name | The name shown everywhere else in the app (this is the record's ID — no separate code). |
| Customer | The ERPNext Customer this site belongs to. |
| Timezone | Used for timestamping decisions correctly. |
| Is Active | Untick to retire a site without deleting its history. |
| Address / Location | Free\-text note, for reference on printouts. |

*Demo data: "Mumbai Yard 1", customer Sanghvi Movers.*

### 3\.2 Log Sheet OCR Template

Describes what a scanned log sheet looks like, for the OCR step.

| Field | Purpose |
| --- | --- |
| Template Name | Identifies the template (also the record ID). |
| Is Active | Only active templates are selectable on a log sheet. |
| Default Confidence Threshold | Below this confidence score, a field is flagged for manual review instead of being trusted automatically. |
| Description | Free text — what kind of sheet this template matches. |
| Expected Field Schema (JSON) | Technical: which fields the OCR provider should return for this layout. |

*Demo data: "Standard Log Sheet v1".*

### 3\.3 Log Sheet Billing Rule

Defines **how billable hours are calculated** from the raw hours on a log sheet — this is what Billing and Operations rely on.

| Field | Purpose |
| --- | --- |
| Rule Name | Identifies the rule (record ID). |
| Rule Version | Printed on the sheet's calculation trace so you always know which rule version produced a number. |
| Is Active | Only active rules are selectable. |
| Bill Idle Hours | Tick to count idle time as billable. |
| Bill Standby Hours | Tick to count standby time as billable. |
| Bill Breakdown Hours | Tick to count breakdown time as billable (usually left unticked — the client shouldn't pay for downtime). |
| Minimum Daily Hours | A floor — if the calculated hours are below this, the client is still billed the minimum. |
| Rounding Increment | Round the final number to the nearest this many hours (e.g. 0.5). |
| Rounding Method | Nearest / Up / Down. |
| Description | Free text explanation of when to use this rule. |

*Demo data: "Standard Demo Rule" — bills idle \+ standby, not breakdown, 8\-hour minimum, rounded to the nearest 0.5 hour.*

### 3\.4 Site Equipment Commercial Mapping

Links a piece of equipment at a site to its SAP commercial references, so the log sheet doesn't need these typed in every day.

| Field | Purpose |
| --- | --- |
| Operating Site | Which site this mapping applies to. |
| Equipment (Asset) | Which crane/asset. |
| Is Active | Untick to retire a mapping. |
| Valid From / Valid To | Date range the mapping is effective — supports contract renewals with different terms. |
| SAP Sales Order / SAP Sales Order Item | The commercial reference SAP validation checks against. |
| Work Order Reference | Optional work order tie\-in. |
| UOM | Unit of measure billed (e.g. Hour). |
| Rate Key | Which rate card line applies. |
| Default Billing Rule | Pre\-fills the Billing Rule on new log sheets for this equipment. |

*Demo data: two mappings, one for the Tower Crane and one for the Mobile Crane, both against demo SAP references.*

### 3\.5 Log Sheet Automation Settings (one record for the whole site)

The control panel for the two external integrations, both of which run in **Mock** mode for this demo so the whole flow can be shown without needing live OCR or SAP credentials.

| Field | Purpose |
| --- | --- |
| OCR Provider Mode | Locked to Google Vision API for this deployment (Mock still exists in code as an offline fallback, but isn't selectable here). |
| Google Service Account Key (JSON) | The one credential OCR needs — paste the full service account key JSON here. |
| OCR Daily Row Pattern (regex) | Tunes how Vision's raw recognized text gets split into one Daily Log row per day — edit this once you've seen real OCR output, no code change needed. |
| OCR Field Label Patterns (JSON) | Secondary tool for matching an optional whole\-sheet total printed as a single labelled value. |
| OCR Timeout (seconds) | How long to wait for the OCR provider before failing. |
| Mock OCR Low\-Confidence Threshold | Below this score, an extracted Daily Log row is marked low\-confidence and routes the sheet to AI Review. |
| SAP Provider Mode | Locked to Live for this deployment. |
| Mock SAP Scenario | Lets you demo the three SAP outcomes on demand: **PASS**, **EXPIRED\_SO**, **INSUFFICIENT\_QTY** — switch this before clicking "Run SAP Validation" to show the exception path. |
| SAP Timeout (seconds) | Timeout for the (future) live SAP call. |
| SAP Endpoint / Auth Type / Credential | Only used once Provider Mode \= Live. |
| Client Token TTL (hours) | How long a client approval link stays valid before it expires. |

## 4\. The Equipment Log Sheet — field by field

This is the main record. Its fields are grouped into sections that match the business process, and most of the workflow\-state fields are **read\-only** — they are set automatically by the system as the sheet moves through its stages, not typed in by a user.

### 4\.1 Status strip (top of every sheet)

| Field | What it shows |
| --- | --- |
| Workflow State | The single "where is this sheet right now" indicator — Draft, AI Review, Ready for Client, Client Approval Pending, Parallel Validation, Operator Rework, Sales Action Pending, Operations Review, Billing Ready, Closed, or Void. |
| Current Owner Role | Whose desk the sheet is sitting on right now (e.g. "Log Sheet Maintenance Approver"). |
| State Entered On | Timestamp of the last state change — how long it's been sitting there. |

### 4\.2 Header

| Field | Notes |
| --- | --- |
| Sheet Template | **Weekly** or **Monthly** — which real paper template this record represents; the form shows/hides a few fields below depending on which. |
| Log Sheet No. | Printed sheet number (the Weekly template numbers each sheet; Monthly doesn't print one). |
| Month / Period Start Date / Period End Date | The sheet's identity — one sheet per site \+ equipment \+ template \+ period start date (the system blocks duplicates for the same combination). |
| Operating Site | Which yard. |
| Customer | Auto\-filled from the site. |
| Equipment | Which crane (an ERPNext Asset). |
| Operator | Who is logging the period. |

### 4\.3 Source Document / OCR

| Field | Notes |
| --- | --- |
| Source Document (scan/photo) | Attach the paper log sheet photo/scan here (optional in the demo). |
| OCR Template | Which layout to read it as. |
| OCR Status | Not Run → Queued → Completed / Failed — set automatically. |
| OCR Provider Result ID | Reference ID from the OCR run, for traceability. |
| OCR Review Complete | The operator ticks this once they've eyeballed any low\-confidence fields OCR flagged. |

### 4\.4 Daily Log and Utilization

The **Daily Log** (`daily_rows`) table is where the actual hours live now — one row per day on the printed sheet, either typed by the operator or filled in by OCR: Day, Date, Working Time From/To, Total Hours, Normal Shift Hours, Overtime Hours, Breakdown Hours (Weekly template only), signature checkboxes, and a Work Description. Every hour field on every row is validated to be between 0 and 24.

The **Utilization** section just below it is now **computed, read\-only** — summed straight from the Daily Log every time the sheet is saved:

| Field | Notes |
| --- | --- |
| Working Hours | Sum of Total Hours across every Daily Log row. |
| Idle Hours / Standby Hours | Always 0 — neither real paper template tracks these as a distinct value; the fields are kept on the schema so nothing downstream (billing calculation, SAP payload) needed to change. |
| Breakdown Hours | Sum of Breakdown Hours across every Daily Log row. |
| Overtime Hours | Sum of Overtime Hours across every Daily Log row. |
| Operator Remarks | Free text. |

### 4\.5 Breakdown Intervals (table)

If there was a breakdown, each individual stoppage can optionally be logged here with more detail than the Daily Log's per\-day Breakdown Hours total — from/to time, duration, a reason code (Mechanical / Electrical / Hydraulic / Operator / Other), which component failed, remarks, and optionally a photo of the fault.

### 4\.6 Commercial Mapping

Auto\-filled from the Site Equipment Commercial Mapping when the sheet is created: SAP Sales Order, SAP Sales Order Item, Work Order Reference, UOM, Rate Key. These are read\-only on the sheet — they come from the master, not typed per sheet.

### 4\.7 Client Approval

| Field | Notes |
| --- | --- |
| Client Recipient | Who the approval link was sent to (email/phone label, for reference — this demo does not send real email/SMS). |
| Client Approval Status | Not Requested → Pending → Approved / Rejected / Expired. |
| Client Decision On | Timestamp of the client's decision. |
| Client Token Expiry | When the current approval link stops working. |
| Client Snapshot Hash | Technical — a fingerprint of exactly what the client saw and approved, so any later edit to the hours is detectable. |
| Client Comment | The client's own remarks (mandatory if they reject). |

### 4\.8 Parallel Validation — Maintenance \+ SAP \+ Operations

The heart of the workflow: two gates open **at the same time** once the client approves, and a third gate (Operations) sits after both.

| Field | Notes |
| --- | --- |
| Maintenance Status | Not Started → Pending → Approved / Rejected. |
| Maintenance Reason Code | Mechanical / Electrical / Hydraulic / Operator / Documentation / Other — required if rejected. |
| Maintenance Comment | Required if rejected. |
| SAP Validation Status | Not Started → Pending → Passed / Exception / Failed. |
| SAP Current Run ID | Reference to the latest SAP check run. |
| Operations Status | Not Started → Pending → Approved / Returned. |
| Operations Comment | Required if returned for rework. |
| Validation Results (table) | The line\-by\-line detail of every OCR and SAP check that has ever run against this sheet — check code, pass/exception/fail, message, and timestamp. This is your audit trail for "why did SAP flag this." |

### 4\.9 Calculation

| Field | Notes |
| --- | --- |
| Billing Rule | Which rule to apply (defaults from the equipment mapping). |
| Billing Rule Version | Snapshot of the rule's version at calculation time. |
| Billable Hours | The system\-calculated result — read\-only, only the Operations approval step can set it. |
| Calculation Trace (JSON) | Shows the full arithmetic — which hour types were included, the minimum\-hours floor, the rounding — so a billing dispute can be answered from the record itself. |

### 4\.10 Billing

| Field | Notes |
| --- | --- |
| Billing Status | Not Ready → Ready → Held / Closed. |
| Billing Hold Reason | Required if the Billing User places the sheet on hold. |
| Manual SAP Document Reference | The invoice / billing document number in SAP — required to close (unless a Manager overrides). |
| Closed On | Timestamp the sheet was closed. |

### 4\.11 Audit (Approval Event History table)

A running log of every decision made on the sheet — event type, who (or "Guest Client Approver" for the client), what state it moved from/to, the decision, any comment, and the time. Nothing on a Log Sheet Automation record can be changed silently; it's always in this table.

## 5\. Running the demo — step by step

This is the exact sequence to show a client end\-to\-end, using the roles above. In this environment one admin user holds all the roles, so you'll be clicking through every step yourself — in production, different people at different desks would do each one.

**Step 1 — Create the log sheet.** From the workspace, click **New Log Sheet**. Fill in Sheet Template (Weekly/Monthly), Month, Period Start/End Date, Operating Site, Equipment, Operator, and the commercial\-mapping fields (Sales Order/Item, Work Order, UOM, Rate Key), pick a Billing Rule, and save. Workflow State opens as **Draft**.

**Step 2 — Run OCR.** Open the saved sheet and click **Run OCR** (top toolbar button, visible to the Operator role while the sheet is Draft/AI Review). The Google Vision provider (or Mock, offline) fills in the Daily Log with one row per day it read off the scan and logs a validation result per row; the sheet moves to **AI Review** if any row came back low\-confidence.

**Step 3 — Clear AI Review.** Review the Daily Log rows OCR filled in, correct anything wrong, tick **OCR Review Complete** and save once the operator has eyeballed the flagged rows.

**Step 4 — Generate the client approval link.** Click **Generate Client Approval Link**. The system creates a one\-time, time\-limited link and moves the sheet to **Client Approval Pending**. In production this link is emailed/texted to the client; in the demo you open it directly in a private browser tab to show it needs no login.

**Step 5 — Client approves (or rejects) on the public page.** The client sees the key figures (site, equipment, sheet template/month/period, the day\-by\-day table, the hour totals) and two buttons — Approve / Reject. Approving moves the sheet straight into **Parallel Validation** and opens both the Maintenance and SAP gates at once. Rejecting sends it to **Operator Rework** with the client's comment.

**Step 6 — Record the Maintenance decision.** As the Maintenance Approver, click **Record Maintenance Decision**, choose Approved (or Rejected with a reason code and comment), and submit.

**Step 7 — Run SAP validation.** Click **Run SAP Validation**. In Mock mode this checks the Mock SAP Scenario configured in Settings:

- **PASS** — all five checks (sales order date, open quantity, equipment match, work order reference, rate) pass, SAP Validation Status → Passed.
- **EXPIRED\_SO** / **INSUFFICIENT\_QTY** — one check fails, SAP Validation Status → Exception, and the sheet moves to **Sales Action Pending** for the Sales Resolver queue.

**Step 7a — (only on exception) Resolve and re\-validate.** The Sales Resolver fixes the underlying issue (e.g. updates the SAP mapping) and clicks **Request SAP Revalidation**, then re\-runs the check.

**Step 8 — Operations sign\-off.** Once both Maintenance and SAP have passed, the sheet reaches **Operations Review**. Click **Approve (Operations)** — this is also the moment the system calculates **Billable Hours** from the Billing Rule and moves the sheet to **Billing Ready**. (Or **Return (Operations)** with a comment to send it back for rework.)

**Step 9 — Close billing.** As the Billing User, click **Close Billing**, enter the SAP document/invoice reference, and confirm. Workflow State → **Closed**, Billing Status → **Closed**. (Or **Hold Billing** with a reason if something still needs resolving first — the sheet stays open.)

**Step 10 — Show the paper trail.** Open the **Approval Event History** table to walk through every decision in order, and use **Print → Log Sheet Approval Summary** to show the client\-ready one\-page summary (identity, utilization, every decision, the latest SAP checks, and the billing calculation — no internal security fields).

> **Bonus for the demo:** two other log sheets are pre\-loaded (LS\-2026\-00001, LS\-2026\-00002) that already show the exception paths — a SAP exception that was corrected and re\-validated, a Maintenance rejection, and a case where editing a Daily Log row *after* client approval automatically revoked that approval (BR\-007's content\-hash check) and sent the sheet back for a fresh sign\-off. A third (LS\-2026\-00003) is a clean run of the happy path end\-to\-end, already Closed, ready to open and narrate.

## 6\. Oversight dashboard

The **Log Sheet Automation** workspace (left sidebar) is the home screen for the whole module:

- **Oversight** — four live counters: Total Active Logs (everything not yet Closed/Void), Sales Exceptions, Billing Ready, Operator Rework — so a manager can see the queue depth at a glance without opening a single record.
- **Operate** — one\-click shortcuts to New Log Sheet, All Log Sheets, My Drafts, and a filtered list for each queue (Operator Rework, Maintenance Pending, Sales Exceptions, Operations Review, Billing Ready).
- **Masters & Configuration** — shortcuts to every master DocType and the Settings screen.

## 7\. What's deliberately out of scope for this MVP

- Real OCR and real SAP connectivity — **done** since this guide was first written: OCR now calls Google Vision API and SAP validation calls a live, configurable REST endpoint, both behind the same Settings surface described above.
- Automated email/SMS delivery of the client approval link — **done**: Email via the site's Email Account, SMS via Twilio.
- Verifying the OCR Daily Row Pattern against real Vision API output — it's been reviewed against the real paper templates but not yet run through a live Vision API call in this environment; treat extracted rows as needing a human check in AI Review until that's been done (see `Log Sheet Automation Settings > OCR Daily Row Pattern`).
- This build was assembled directly on the ERPNext Desk UI (DocTypes, Server Scripts, Client Scripts, Workspace) rather than as a separate installed, git\-tracked application — see the companion Technical document for what that means and what promoting it to a proper app would involve.
