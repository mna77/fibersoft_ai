import frappe


def execute():
	# messages get a direct user link + counted tokens (used by token limits)
	frappe.db.sql(
		"""update `tabFibersoft AI Message` m join `tabFibersoft AI Conversation` c on c.name = m.conversation
		set m.user = c.user where m.user is null or m.user = ''"""
	)
	frappe.db.sql(
		"""update `tabFibersoft AI Message`
		set billable_tokens = coalesce(input_tokens, 0) + coalesce(output_tokens, 0)
			+ coalesce(cache_write_tokens, 0) + round(coalesce(cache_read_tokens, 0) * 0.1)
		where role = 'assistant' and coalesce(billable_tokens, 0) = 0"""
	)
	# new default branding (only if still on the old defaults)
	if frappe.db.get_single_value("Fibersoft AI Settings", "assistant_name") in (None, "", "Fibersoft ERP Assistant"):
		frappe.db.set_single_value("Fibersoft AI Settings", "assistant_name", "Fibersoft AI")
	if (frappe.db.get_single_value("Fibersoft AI Settings", "theme_color") or "").lower() in ("", "#2563eb"):
		frappe.db.set_single_value("Fibersoft AI Settings", "theme_color", "#111111")
	frappe.cache.delete_key("bootinfo")
