app_name = "fibersoft_ai"
app_title = "Fibersoft AI"
app_publisher = "Fibersoft"
app_description = "Context-aware AI assistant for Fibersoft ERP"
app_email = "support@fibersoft.online"
app_license = "Proprietary"
app_logo_url = "/assets/fibersoft_ai/images/fibersoft-ai.svg"

required_apps = ["frappe"]

# Chat launcher + panel on every desk page
app_include_js = "fibersoft_ai.bundle.js"
app_include_css = "fibersoft_ai.bundle.css"

boot_session = "fibersoft_ai.boot.boot_session"

after_install = "fibersoft_ai.install.after_install"

scheduler_events = {
	"hourly": ["fibersoft_ai.tasks.expire_pending_actions"],
	"daily": ["fibersoft_ai.tasks.purge_old_history"],
}

# Users may only ever see their own conversations / messages / actions in list views
permission_query_conditions = {
	"Fibersoft AI Conversation": "fibersoft_ai.permissions.conversation_query",
	"Fibersoft AI Message": "fibersoft_ai.permissions.message_query",
	"Fibersoft AI Action": "fibersoft_ai.permissions.action_query",
}
has_permission = {
	"Fibersoft AI Conversation": "fibersoft_ai.permissions.has_owner_permission",
	"Fibersoft AI Message": "fibersoft_ai.permissions.has_message_permission",
	"Fibersoft AI Action": "fibersoft_ai.permissions.has_owner_permission",
}
