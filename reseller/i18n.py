"""All text shown in the panel, in plain English.

Codes (error codes, statuses, issue names) map to short sentences that say what
happened and, where useful, what to do next. Unknown keys fall back to a
generic message so internal codes never leak onto the page.
"""

MESSAGES = {
    # ----- Navigation and common words
    'brand': 'Relay', 'brand_sub': 'Reseller panel',
    'settings': 'Settings', 'invalid_sub_url': 'Enter a full address starting with https:// (or http://), for example https://sub.example.com/i/',
    'dashboard': 'Dashboard', 'customers': 'Customers', 'resellers': 'Resellers', 'inbounds': 'Inbounds',
    'activity': 'Activity', 'attention': 'Needs attention', 'account': 'Account',
    'login': 'Sign in', 'logout': 'Sign out', 'username': 'Username', 'password': 'Password',
    'save': 'Save changes', 'create': 'Create', 'cancel': 'Cancel', 'back': 'Back', 'edit': 'Edit',
    'delete': 'Delete', 'details': 'Details', 'search': 'Search', 'all': 'All', 'next': 'Next',
    'previous': 'Previous', 'yes': 'Yes', 'no': 'No', 'copy': 'Copy', 'copied': 'Copied',
    'working': 'Working…', 'empty': 'Nothing here yet.', 'system': 'System', 'admin': 'Admin',
    'reseller': 'Reseller', 'confirm_again': 'Click again to confirm', 'show': 'Show', 'hide': 'Hide',
    'sync_now': 'Sync now', 'theme': 'Theme', 'menu': 'Menu', 'close': 'Close', 'status': 'Status',

    # ----- Statuses shown as tags
    'active': 'Active', 'disabled': 'Disabled', 'expired': 'Expired', 'suspended': 'Suspended',
    'online': 'Online', 'offline': 'Offline', 'setting_up': 'Setting up', 'out_of_data': 'Out of data',
    'deleted': 'Removed', 'failed': 'Failed', 'expiring': 'Expiring soon', 'needs_attention': 'Needs attention',
    'available': 'Available', 'unavailable': 'Unavailable', 'unsupported': 'Not supported',
    'enabled': 'Enabled', 'unknown': 'Unknown', 'no_plan': 'No active plan',
    'pending': 'Waiting', 'committed': 'Done', 'uncertain': 'Checking', 'sent': 'Sent to 3X-UI',
    'reserved': 'On hold', 'verified': 'OK', 'mismatch': 'Different in 3X-UI',

    # ----- Dashboard
    'total_resellers': 'Resellers', 'total_customers': 'Customers', 'online_now': 'Online now',
    'attention_count': 'Need attention', 'plan': 'Plan', 'plan_total': 'Plan size', 'plan_used': 'Sold',
    'plan_hold': 'On hold', 'plan_left': 'Left to sell', 'plan_expires': 'Plan expires',
    'days_left': 'days left', 'quota_hint': 'Selling a customer uses your plan right away. Customer traffic is shown separately.',
    'connection': '3X-UI connection', 'last_sync': 'Last sync', 'never': 'Never',
    'recent_activity': 'Recent activity', 'see_all': 'See all', 'customers_enabled': 'Enabled',
    'quick_actions': 'Quick actions', 'total_granted': 'Total data given', 'total_sold': 'Total data sold',
    'synced_ago': 'Synced', 'sync_failing': '3X-UI unreachable', 'sync_waiting': 'Waiting for first sync',

    # ----- Customers
    'new_customer': 'New customer', 'customer': 'Customer', 'email': 'Name / email',
    'email_hint': 'A unique name for this customer, like ali-phone. Letters, numbers, dot, dash, underscore or @.',
    'comment': 'Note', 'comment_hint': 'Optional. Only you and the admin see this.',
    'volume': 'Data limit (GB)', 'usage': 'Used', 'remaining': 'Left', 'package': 'Data limit',
    'expiry': 'Expires', 'first_use': 'Start counting days from first connection',
    'days': 'Valid for (days)', 'expiry_date': 'Expiry date', 'owner': 'Reseller',
    'inbounds_for_customer': 'Inbounds', 'pick_inbounds': 'Pick one or more inbounds. They must all use the same protocol.',
    'advanced': 'Advanced (optional)', 'advanced_hint': 'Leave empty and Relay creates these for you.',
    'uuid': 'UUID', 'sub_id': 'Subscription ID', 'credential': 'Password', 'keep_credential': 'Leave empty to keep the current value.',
    'links': 'Connection links', 'subscription_link': 'Subscription link',
    'subscription_hint': 'Give this link to the customer. Their app keeps itself up to date with it.',
    'no_subscription': 'No subscription address is set yet. The admin can add it under Settings.',
    'config_uris': 'Single configs', 'config_hint': 'One link per inbound. Use these if the app has no subscription support.',
    'no_links': 'No connection links yet. They appear after 3X-UI confirms the customer.',
    'enable_customer': 'Customer can connect', 'last_update': 'Last update', 'online_status': 'Connection',
    'expires_in': 'in', 'days_unit': 'days', 'day_unit': 'day', 'unlimited': 'Unlimited',
    'starts_on_first_use': 'Starts on first use', 'customer_basics': 'Customer', 'data_and_time': 'Data and time',
    'remaining_after': 'Left in your plan after this', 'quota_available': 'Left in your plan',
    'delete_customer': 'Delete customer', 'delete_customer_hint': 'Removes this customer from 3X-UI. Data already sold is not returned to your plan.',
    'removed_inbound': 'removed in 3X-UI', 'no_inbounds_left': 'This customer has no inbounds left (they were removed in 3X-UI). Edit the customer to add one.',
    'filter_all': 'All', 'filter_active': 'Active', 'filter_disabled': 'Disabled', 'filter_expired': 'Expired',
    'filter_online': 'Online', 'filter_attention': 'Needs attention', 'filter_removed': 'Removed',
    'search_customers': 'Search name or note', 'no_customers': 'No customers yet.', 'no_results': 'No customers match these filters.',
    'customer_history': 'Changes', 'operations': 'Changes',

    # ----- Resellers
    'new_reseller': 'New reseller', 'tag': 'Group tag', 'tag_hint': 'Shown as the client group in 3X-UI. It cannot be changed later.',
    'notes': 'Admin notes', 'reset_password': 'New password', 'reset_password_hint': 'Optional. The reseller must change it at next sign-in.',
    'allowed': 'Allowed inbounds', 'allowed_hint': 'The reseller can only create customers on these inbounds.',
    'renew': 'Start a new plan', 'renew_hint': 'Replaces the current plan. Already sold data stays with the old plan.',
    'plan_adjust': 'Change current plan', 'plan_adjust_hint': 'Change size or end date of the active plan without starting a new one.',
    'history': 'Plan history', 'ledger': 'Data movements', 'granted': 'Size', 'sold': 'Sold', 'lifetime': 'Sold in total',
    'created': 'Created', 'suspend': 'Suspend reseller', 'suspend_hint': 'Suspended resellers cannot sign in, and their customers are disabled until you turn this off.',
    'danger_zone': 'Danger zone', 'delete_reseller': 'Delete reseller and customers',
    'delete_reseller_hint': 'Deletes this reseller and all of their {n} customers from Relay and 3X-UI. This cannot be undone.',
    'overview': 'Overview', 'plan_tab': 'Plan', 'search_resellers': 'Search resellers', 'no_resellers': 'No resellers yet.',
    'sort': 'Sort', 'sort_username': 'Name', 'sort_created': 'Newest', 'sort_sold': 'Most sold',
    'filter_suspended': 'Suspended', 'expired_plan': 'Plan expired', 'reseller_customers': 'Customers',

    # ----- Inbounds
    'protocol': 'Protocol', 'port': 'Port', 'synced': 'Last seen', 'inbound_name': 'Name',
    'unnamed_inbound': 'Unnamed inbound', 'inbounds_hint': 'Inbounds come from 3X-UI. Ones deleted there are removed here on the next sync.',
    'customers_count': 'Customers',

    # ----- Account / login
    'welcome': 'Welcome back', 'login_hint': 'Sign in to manage your customers.',
    'current_password': 'Current password', 'new_password': 'New password',
    'password_policy': 'Use 12 to 256 characters.', 'must_change': 'Please choose your own password before you continue.',
    'change_password': 'Change password', 'signed_in_as': 'Signed in as',

    # ----- Needs attention
    'attention_hint': 'Customers and changes Relay could not finish on its own. Each item says what happened and what you can do.',
    'all_clear': 'All clear. Nothing needs your attention.',
    'accept_remote': 'Accept 3X-UI values', 'accept_remote_hint': 'Relay copies what 3X-UI has now. The reseller is not charged or refunded.',
    'check_again': 'Check again', 'retry_send': 'Send again', 'mark_done': 'Mark as done',
    'remove_customer': 'Remove customer', 'retry_unsent': 'Try again',
    'field': 'Field', 'relay_value': 'In Relay', 'xui_value': 'In 3X-UI', 'technical': 'Technical details',
    'what_changed': 'What changed in 3X-UI', 'started': 'Started',
    'issue_remote_changed': 'Someone changed this customer directly in 3X-UI, so Relay paused it to avoid overwriting that change. Review the differences, then accept the 3X-UI values or remove the customer.',
    'issue_remote_identity': 'This customer was replaced in 3X-UI (its ID or subscription changed). Accept the 3X-UI values if this is the same customer, otherwise remove it.',
    'issue_remote_drift': 'This customer was changed directly in 3X-UI. Accept the 3X-UI values or remove the customer.',
    'issue_remote_missing': 'This customer no longer exists in 3X-UI.',
    'issue_suspension_pending': 'Waiting for 3X-UI to turn this customer on or off after a reseller suspension change. Relay retries automatically.',
    'issue_setting_up': 'Relay sent this change to 3X-UI and is waiting to confirm it worked. If the customer was deleted in 3X-UI, Relay removes it here after about 1–2 minutes.',
    'issue_node_pending': '3X-UI saved the change, but a node server has not confirmed it yet. Relay accepts it automatically after a minute.',
    'issue_failed_create': 'Creating this customer failed before anything was sent to 3X-UI. No data was used from the plan.',
    'reseller_view_issue': 'The admin needs to review this customer. You can still delete it.',

    # ----- Flash messages (green)
    'saved': 'Changes saved.', 'submitted': 'Done. Relay is applying the change in 3X-UI.',
    'customer_created': 'Customer created. Usage and online status appear within a minute.',
    'customer_activation_pending': 'Customer sent to 3X-UI. Relay is confirming it; this usually takes under a minute.',
    'customer_create_failed': 'The customer could not be created. See the message below for the reason.',
    'customer_updated': 'Customer updated.', 'customer_deleted': 'Customer deleted.',
    'customer_delete_pending': 'Delete sent to 3X-UI. Relay is confirming it.',
    'suspension_sync_pending': 'Saved. Some customers are still being turned on or off in 3X-UI; Relay keeps retrying.',
    'synced': 'Sync finished.', 'accepted': 'Relay now uses the values from 3X-UI.',
    'customer_removed': 'Customer removed.', 'reseller_deleted': 'Reseller and their customers were deleted.',
    'marked_done': 'Marked as done.', 'password_changed': 'Password changed. Other devices were signed out.',
    'reseller_created': 'Reseller created.', 'plan_renewed': 'New plan started.', 'plan_changed': 'Plan updated.',

    # ----- Errors (red): what happened + what to do
    'error': 'Something went wrong. Please try again.',
    'invalid_login': 'Wrong username or password, or too many attempts. Wait a few minutes and try again.',
    'forbidden': "You don't have access to this.",
    'not_found': 'This item does not exist or was deleted.',
    'session_expired': 'Your session expired. Please try again.',
    'csrf': 'Your session expired. Please try again.',
    'too_large': 'The form was too large to send.',
    'rate_limited': "You're going too fast. Wait a minute and try again.",
    'invalid_input': 'Some values are not valid. Check the form and try again.',
    'invalid_volume': 'Enter a data limit above 0 GB.',
    'invalid_expiry': 'Pick a date in the future, or a number of days from 1 to 3650.',
    'invalid_identifier': 'Use 2–64 characters: letters, numbers, dot, dash, underscore or @. No spaces.',
    'insufficient_quota': 'Not enough data left in your plan for this.',
    'plan_expired': 'This plan has expired, so new sales and changes are paused. Ask the admin to renew it.',
    'inbound_denied': 'You are not allowed to use one of the selected inbounds.',
    'inbound_unavailable': 'One of the selected inbounds is turned off or was removed in 3X-UI. Pick another one.',
    'no_inbound': 'Pick at least one inbound.',
    'identity_conflict': 'This name, UUID or subscription ID is already used by another customer. Choose a different one.',
    'operation_conflict': 'This form was already sent. Reload the page and try again.',
    'invalid_operation': 'This form is out of date. Reload the page and try again.',
    'mixed_protocols': 'All selected inbounds must use the same protocol.',
    'password_policy': 'The password must be 12 to 256 characters long.',
    'wrong_password': 'The current password is wrong.',
    'remote_unavailable': "Can't reach 3X-UI right now. Nothing was lost; try again in a moment.",
    'remote_unconfigured': 'The 3X-UI connection is not set up yet.',
    'remote_auth': '3X-UI refused the login. The admin needs to check the 3X-UI token or password.',
    'remote_rejected': '3X-UI refused the change. Try again or ask the admin.',
    'remote_schema': '3X-UI sent an answer Relay does not understand. Update Relay to the latest version, or tell the Relay developers your 3X-UI version.',
    'remote_version': '3X-UI sent an answer Relay does not understand. Update Relay to the latest version.',
    'remote_not_found': 'This 3X-UI does not have the feature Relay asked for. Update Relay or 3X-UI.',
    'single_inbound': 'Your 3X-UI version allows each customer on one inbound only. Pick a single inbound (or update 3X-UI to 3.1 or newer for several).',
    'xui_mode_clients': 'client API', 'xui_mode_classic': 'classic API (one inbound per customer)',
    'reconcile_required': 'This customer is still being updated in 3X-UI. Wait a minute and try again.',
    'remote_missing': 'This customer no longer exists in 3X-UI.',
    'remote_changed': 'This customer was changed directly in 3X-UI. The admin must review it first.',
    'remote_identity': 'This customer was replaced in 3X-UI. The admin must review it first.',
    'remote_drift': 'This customer was changed directly in 3X-UI. The admin must review it first.',
    'suspension_pending': 'Waiting for 3X-UI to apply a suspension change. Try again in a minute.',
    'restore_locked': 'Sales are paused after a backup restore. The admin must unlock them.',
    'sync_busy': 'A sync is already running. Try again in a few seconds.',
    'sync_too_soon': 'Sync ran a moment ago. Wait a few seconds.',
    'reseller_delete_partial': 'Some customers could not be deleted from 3X-UI, so the reseller was kept. Try again when 3X-UI is reachable.',
    'node_pending': 'Waiting for a node server to confirm.',
    'unsent_expired': 'Cancelled before it was sent.',
    'cancelled': 'Cancelled by the admin.',
    'remote_ack': '3X-UI accepted it; confirming.',

    # ----- Bulk inbound changes
    'bulk_title': 'Change many customers', 'bulk_button': 'Change inbounds', 'bulk_all_matching': 'Change all {n} customers in this list',
    'bulk_hint': 'Add or remove inbounds for many customers at once. You see a preview first; nothing changes until you apply it.',
    'bulk_by_filter': 'Picked by the list filters', 'bulk_all': 'All customers', 'bulk_what': 'What should happen?',
    'bulk_add': 'Add inbounds', 'bulk_remove': 'Remove inbounds', 'bulk_move': 'Move to', 'bulk_enable': 'Turn on', 'bulk_disable': 'Turn off',
    'bulk_explain_add': 'The chosen inbounds are added. Customers keep the inbounds they already have. Only inbounds with the same protocol as the customer are added.',
    'bulk_explain_remove': 'The chosen inbounds are removed. A customer always keeps at least one inbound.',
    'bulk_explain_move': 'Customers get exactly the chosen inbounds instead of their current ones (same protocol only). Handy when you replace a server.',
    'bulk_explain_enable': 'Customers can connect again.', 'bulk_explain_disable': 'Customers cannot connect until you turn them on again. Their data and time are kept.',
    'bulk_no_inbounds': 'No inbounds you can use.', 'bulk_preview': 'Preview', 'bulk_will_change': 'Will change', 'bulk_will_skip': 'Skipped',
    'bulk_time': 'About', 'bulk_before': 'Now', 'bulk_after': 'After', 'bulk_apply': 'Apply to {n} customers',
    'bulk_running': 'Running', 'bulk_finished': 'Finished', 'bulk_interrupted_tag': 'Stopped',
    'bulk_running_hint': 'You can leave this page; the changes continue in the background.',
    'bulk_done': 'Changed', 'bulk_skipped': 'Skipped', 'bulk_nothing': 'Nothing to change for these customers.',
    'bulk_selected': 'selected', 'bulk_clear': 'Clear', 'bulk_select_page': 'Select all on this page',
    'bulk_not_active': 'not active (removed, failed or still being set up)', 'bulk_attention': 'needs the admin\'s attention first',
    'bulk_in_progress': 'another change is still running', 'bulk_owner_suspended': 'the reseller is suspended',
    'bulk_plan_expired': 'the reseller\'s plan has expired', 'bulk_already': 'already like that',
    'bulk_different_protocol': 'uses a different protocol', 'bulk_not_on_inbound': 'is not on these inbounds',
    'bulk_last_inbound': 'would have no inbound left', 'bulk_inbound_denied': 'the reseller may not use this inbound',
    'bulk_confirming': '3X-UI is confirming', 'bulk_interrupted': 'not changed (Relay restarted)',
    'add_to_customers': 'Add to customers', 'remove_from_customers': 'Remove from customers',

    # ----- Requests and Telegram
    'request_title': 'Request more data', 'request_hint': 'Ask the admin to add data to your plan, and more days if you need them. You get an answer here and in Telegram.',
    'request_volume': 'How much data', 'request_days': 'Extra days', 'request_days_hint': '0 keeps the current end date.',
    'request_note_hint': 'Optional, e.g. "many new customers this month"', 'request_send': 'Send request',
    'request_sent': 'Request sent. The admin has been notified.', 'request_pending': 'You already have a request waiting for the admin.',
    'request_decided': 'This request was already answered.', 'request_waiting': 'Your request for {gb} is waiting for the admin.',
    'request_was_approved': 'Your request for {gb} was approved.', 'request_was_declined': 'Your request for {gb} was declined.',
    'request_asks': 'asks for more data', 'request_tag': 'Request', 'request_plan_now': 'Plan now', 'request_no_plan': 'No active plan; approving starts a new one.',
    'request_answer': 'Message to the reseller (optional)', 'request_answer_hint': 'e.g. "Approved for this month"',
    'request_approve': 'Approve', 'request_decline': 'Decline',
    'request_approved_msg': 'Request approved; the plan was topped up.', 'request_declined_msg': 'Request declined.',
    'telegram_off': 'The Telegram bot is turned off. The admin can turn it on under Settings.',
    'telegram_unreachable': 'Relay cannot reach Telegram with this token. Check the token, or set a proxy if Telegram is blocked on this server.',
    'telegram_not_linked': 'Connect your own Telegram on the Account page first.', 'telegram_bad_token': 'This does not look like a bot token. It looks like 123456789:AAE…',
    'telegram_bad_proxy': 'The proxy must start with socks5://, http:// or https://.',
    'telegram_bad_api': 'The Telegram API address must be an https:// address, e.g. https://tg.example.com.', 'telegram_unlinked': 'Telegram disconnected.',
    'telegram_test_sent': 'Test message sent. Check Telegram.',

    # ----- Change (operation) kinds
    'op_create': 'Create', 'op_update': 'Edit', 'op_delete': 'Delete',
}


def translator(lang=None):
    def translate(key):
        return MESSAGES.get(str(key), MESSAGES['error'] if key else '')
    return translate


def known(key):
    return str(key) in MESSAGES


# Kept for older imports.
PAIRS = MESSAGES
