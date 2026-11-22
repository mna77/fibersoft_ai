import frappe


def _is_manager(user):
	return "System Manager" in frappe.get_roles(user)


def _owner_condition(doctype, user):
	user = user or frappe.session.user
	if _is_manager(user):
		return ""
	return "`tab{0}`.`user` = {1}".format(doctype, frappe.db.escape(user))


def conversation_query(user=None):
	return _owner_condition("Fibersoft AI Conversation", user)


def action_query(user=None):
	return _owner_condition("Fibersoft AI Action", user)


def message_query(user=None):
	user = user or frappe.session.user
	if _is_manager(user):
		return ""
	return (
		"`tabFibersoft AI Message`.`conversation` in "
		"(select name from `tabFibersoft AI Conversation` where `user` = {0})"
	).format(frappe.db.escape(user))


def has_owner_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if _is_manager(user):
		return True
	return doc.get("user") == user


def has_message_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if _is_manager(user):
		return True
	return frappe.db.get_value("Fibersoft AI Conversation", doc.conversation, "user") == user
