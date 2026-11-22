import frappe
from frappe.utils import add_days, add_to_date, cint, now_datetime, nowdate

from fibersoft_ai.ai.settings import get_settings


def expire_pending_actions():
	hours = cint(get_settings().action_expiry_hours) or 24
	cutoff = add_to_date(now_datetime(), hours=-hours)
	frappe.db.set_value(
		"Fibersoft AI Action", {"status": "Pending", "creation": ["<", cutoff]}, "status", "Expired", update_modified=False
	)
	frappe.db.commit()


def purge_old_history():
	days = cint(get_settings().log_retention_days)
	if not days:
		return
	cutoff = add_days(nowdate(), -days)
	old = frappe.get_all("Fibersoft AI Conversation", filters={"last_message_on": ["<", cutoff]}, pluck="name", limit=5000)
	for name in old:
		frappe.delete_doc("Fibersoft AI Conversation", name, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
