# Copyright (c) 2026, Fibersoft and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class FibersoftAISettings(Document):
	def validate(self):
		self.model = (self.model or "").strip()
		if not self.model:
			frappe.throw(_("Model is required"))
		self.api_base_url = (self.api_base_url or "https://api.anthropic.com").strip().rstrip("/")
		local = self.api_base_url.startswith(("http://127.0.0.1", "http://localhost"))
		if not (self.api_base_url.startswith("https://") or local):
			frappe.throw(_("API Base URL must start with https:// (plain http is only allowed for a local gateway on this server)"))

		self.max_tokens = min(max(cint(self.max_tokens) or 4096, 512), 64000)
		self.max_tool_iterations = min(max(cint(self.max_tool_iterations) or 15, 1), 40)
		self.request_timeout = min(max(cint(self.request_timeout) or 120, 15), 600)
		self.history_messages = min(max(cint(self.history_messages), 0), 100)
		self.action_expiry_hours = max(cint(self.action_expiry_hours) or 24, 1)
		if cint(self.log_retention_days) and cint(self.log_retention_days) < 35:
			frappe.throw(_("Keep Chat History must be at least 35 days (or 0 = forever), so monthly token limits stay accurate"))
		for fieldname in ("input_cost_per_million", "output_cost_per_million", "monthly_budget_usd"):
			if flt(self.get(fieldname)) < 0:
				frappe.throw(_("{0} cannot be negative").format(self.meta.get_label(fieldname)))

		seen = set()
		for row in self.action_rules or []:
			if row.document_type in seen:
				frappe.throw(_("Row {0}: {1} is listed twice in Action Rules").format(row.idx, row.document_type))
			seen.add(row.document_type)
			from fibersoft_ai.ai.guard import ALWAYS_READ_ONLY_DOCTYPES

			if row.document_type in ALWAYS_READ_ONLY_DOCTYPES:
				frappe.throw(
					_("Row {0}: {1} is a system DocType and cannot be changed by the assistant").format(
						row.idx, row.document_type
					)
				)

		for row in self.redacted_fields or []:
			if not frappe.get_meta(row.document_type).has_field(row.fieldname):
				frappe.throw(
					_("Redacted Fields row {0}: {1} has no field {2}").format(row.idx, row.document_type, row.fieldname)
				)

	def on_update(self):
		# the launcher reads its config from boot info
		frappe.cache.delete_key("bootinfo")
		frappe.cache.delete_value("fibersoft_ai_settings")
