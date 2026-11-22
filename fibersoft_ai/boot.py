import frappe
from frappe.utils import cint

from fibersoft_ai import __version__
from fibersoft_ai.ai.settings import actions_enabled, get_settings, user_is_allowed


def widget_config():
	s = get_settings()
	allowed = user_is_allowed()
	cfg = {"allowed": allowed}
	if not allowed:
		return cfg
	cfg.update({
		"assistant_name": s.assistant_name or "Fibersoft AI",
		"welcome_message": s.welcome_message or "",
		"icon": s.launcher_icon or f"/assets/fibersoft_ai/images/fibersoft-ai.svg?v={__version__}",
		"theme_color": s.theme_color or "#111111",
		"position": "left" if s.launcher_position == "Bottom Left" else "right",
		"voice": cint(s.enable_voice),
		"actions": actions_enabled(),
		"is_manager": "System Manager" in frappe.get_roles(),
		"configured": bool(s.get_password("api_key", raise_exception=False)),
	})
	return cfg


def boot_session(bootinfo):
	try:
		if frappe.session.user == "Guest":
			return
		bootinfo.fibersoft_ai = widget_config()
	except Exception:
		# never break desk loading because of the assistant
		frappe.log_error(title="Fibersoft AI boot failed")
		bootinfo.fibersoft_ai = {"allowed": False}
