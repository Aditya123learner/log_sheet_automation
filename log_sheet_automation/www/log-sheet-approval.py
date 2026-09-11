# Copyright (c) 2026, Logic Motive Consultant and contributors
# For license information, please see license.txt

"""Context provider for the public client approval page. The page itself
does all its data fetching client-side against the guest-whitelisted API
methods (get_log_sheet_approval_snapshot / record_log_sheet_client_decision)
so this only needs to mark the route as a no-login page.
"""

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.title = "Log Sheet Approval"
