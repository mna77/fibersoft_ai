# Copyright (c) 2026, Fibersoft and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class FibersoftAITokenLimit(Document):
	def validate(self):
		if self.limit_for == "User":
			if not self.user:
				frappe.throw(_("User is required"))
			self.role, self.priority = None, 0
			self.title = _("User: {0}").format(frappe.db.get_value("User", self.user, "full_name") or self.user)
			self._unique({"limit_for": "User", "user": self.user}, _("a rule for user {0}").format(self.user))
		elif self.limit_for == "Role":
			if not self.role:
				frappe.throw(_("Role is required"))
			self.user = None
			self.title = _("Role: {0}").format(self.role)
			self._unique({"limit_for": "Role", "role": self.role}, _("a rule for role {0}").format(self.role))
		else:
			self.user, self.role, self.priority = None, None, 0
			self.title = _("Everyone")
			self._unique({"limit_for": "Everyone"}, _("an Everyone rule"))

		for f in ("daily_token_limit", "weekly_token_limit", "monthly_token_limit", "daily_question_limit"):
			if cint(self.get(f)) < 0:
				frappe.throw(_("{0} cannot be negative").format(self.meta.get_label(f)))
		if flt(self.monthly_cost_limit_usd) < 0:
			frappe.throw(_("Monthly Cost Limit cannot be negative"))
		d, w, m = cint(self.daily_token_limit), cint(self.weekly_token_limit), cint(self.monthly_token_limit)
		if d and w and d > w:
			frappe.throw(_("Daily Tokens cannot be more than Weekly Tokens"))
		if w and m and w > m:
			frappe.throw(_("Weekly Tokens cannot be more than Monthly Tokens"))
		if d and m and d > m:
			frappe.throw(_("Daily Tokens cannot be more than Monthly Tokens"))
		self.model = (self.model or "").strip() or None

	def _unique(self, filters, what):
		filters = dict(filters, name=["!=", self.name])
		other = frappe.db.get_value("Fibersoft AI Token Limit", filters, "name")
		if other:
			frappe.throw(_("There is already {0}: {1}").format(what, frappe.utils.get_link_to_form(self.doctype, other)))
