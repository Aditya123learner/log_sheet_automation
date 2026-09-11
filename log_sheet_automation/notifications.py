# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Delivers the client approval link once it's generated.

Reads `Log Sheet Automation Settings.notification_channel` to decide
whether/how to send: Email uses Frappe's own outgoing Email Account (so it
queues through whatever SMTP is already configured on the site — no new
credentials needed beyond what's already set up in Email Account); SMS uses
Twilio's REST API directly (the only gateway wired up here — add another
branch in `_send_sms` if you use a different provider).

A delivery failure here must never block the workflow: the approval link
was already generated and saved on the document before this is called, so
on any notification error we log it, record it on the log sheet's own
audit trail, and return normally rather than raising.
"""

import frappe
from frappe.utils import get_url


def send_client_approval_link(doc, approval_path, expiry):
	"""doc: Equipment Log Sheet. approval_path: e.g. "/log-sheet-approval?token=...".
	Returns a short status string that's safe to fold into the audit trail
	comment; never raises.
	"""
	settings = frappe.get_cached_doc("Log Sheet Automation Settings")
	channel = settings.notification_channel or "Email"

	if channel == "None":
		return "Notification channel is 'None' — link was not sent, only returned to the caller."
	if not doc.client_recipient:
		return "No Client Recipient set on the log sheet — nothing to send to."

	full_url = get_url(approval_path)
	results = []

	if channel in ("Email", "Email and SMS") and "@" in doc.client_recipient:
		results.append(_send_email(doc, settings, full_url, expiry))
	if channel in ("SMS", "Email and SMS") and _looks_like_phone(doc.client_recipient):
		results.append(_send_sms(doc, settings, full_url, expiry))

	if not results:
		return f"Notification channel is '{channel}' but Client Recipient ('{doc.client_recipient}') doesn't look like a matching address/number — nothing sent."

	return " ".join(results)


def _looks_like_phone(value):
	digits = "".join(ch for ch in value if ch.isdigit())
	return len(digits) >= 7 and "@" not in value


def _send_email(doc, settings, full_url, expiry):
	try:
		frappe.sendmail(
			recipients=[doc.client_recipient],
			subject=f"Action needed: approve log sheet {doc.name} ({doc.log_date})",
			message=_email_body(doc, full_url, expiry),
			sender_name=settings.notification_sender_name or "Log Sheet Automation",
			now=True,
		)
		return "Email queued."
	except Exception:
		frappe.log_error(title="Log Sheet Automation: email delivery failed", message=frappe.get_traceback())
		return "Email delivery failed — see Error Log."


def _email_body(doc, full_url, expiry):
	return f"""
	<p>A crane log sheet is ready for your approval.</p>
	<table style="border-collapse:collapse">
		<tr><td style="padding:2px 8px;color:#666">Site</td><td style="padding:2px 8px"><b>{frappe.utils.escape_html(doc.operating_site or "")}</b></td></tr>
		<tr><td style="padding:2px 8px;color:#666">Equipment</td><td style="padding:2px 8px"><b>{frappe.utils.escape_html(doc.equipment or "")}</b></td></tr>
		<tr><td style="padding:2px 8px;color:#666">Date</td><td style="padding:2px 8px"><b>{frappe.utils.escape_html(str(doc.log_date))}</b></td></tr>
		<tr><td style="padding:2px 8px;color:#666">Shift</td><td style="padding:2px 8px"><b>{frappe.utils.escape_html(doc.shift or "")}</b></td></tr>
	</table>
	<p style="margin-top:16px">
		<a href="{full_url}" style="background:#2e7d32;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none">Review &amp; Approve</a>
	</p>
	<p style="font-size:12px;color:#888">This link expires {expiry} and does not require you to log in.</p>
	"""


def _send_sms(doc, settings, full_url, expiry):
	if settings.sms_gateway_provider != "Twilio":
		return "SMS requested but no SMS gateway provider is configured."

	account_sid = settings.twilio_account_sid
	auth_token = settings.get_password("twilio_auth_token", raise_exception=False)
	from_number = settings.twilio_from_number
	if not (account_sid and auth_token and from_number):
		return "SMS requested but Twilio credentials are incomplete in Settings."

	try:
		import requests

		body = f"Log sheet {doc.name} needs your approval: {full_url} (expires {expiry})"
		resp = requests.post(
			f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
			auth=(account_sid, auth_token),
			data={"From": from_number, "To": doc.client_recipient, "Body": body},
			timeout=15,
		)
		resp.raise_for_status()
		return "SMS sent via Twilio."
	except Exception:
		frappe.log_error(title="Log Sheet Automation: SMS delivery failed", message=frappe.get_traceback())
		return "SMS delivery failed — see Error Log."
