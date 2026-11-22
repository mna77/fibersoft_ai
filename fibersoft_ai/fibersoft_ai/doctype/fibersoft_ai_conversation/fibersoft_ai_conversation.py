# Copyright (c) 2026, Fibersoft and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FibersoftAIConversation(Document):
	def on_trash(self):
		frappe.db.delete("Fibersoft AI Message", {"conversation": self.name})
		frappe.db.delete("Fibersoft AI Action", {"conversation": self.name, "status": ["!=", "Executed"]})
		frappe.db.set_value(
			"Fibersoft AI Action", {"conversation": self.name}, "conversation", None, update_modified=False
		)
