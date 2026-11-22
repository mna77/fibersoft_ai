import frappe
from frappe import _
from frappe.utils import cint, flt, get_first_day, getdate, nowdate

DOCTYPE = "Fibersoft AI Settings"


def get_settings():
	"""Settings doc, cached for the request."""
	if not getattr(frappe.local, "fibersoft_ai_settings", None):
		frappe.local.fibersoft_ai_settings = frappe.get_cached_doc(DOCTYPE)
	return frappe.local.fibersoft_ai_settings


def clear_local_cache():
	frappe.local.fibersoft_ai_settings = None


def get_api_key():
	s = get_settings()
	return s.get_password("api_key", raise_exception=False) or ""


def user_is_allowed(user=None):
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	s = get_settings()
	if not cint(s.enabled):
		return False
	if frappe.db.get_value("User", user, "user_type") != "System User":
		return False
	roles = {r.role for r in (s.allowed_roles or [])}
	if not roles:
		return True
	return bool(roles & set(frappe.get_roles(user)))


def action_rule(doctype):
	s = get_settings()
	if not cint(s.allow_actions):
		return None
	for row in s.action_rules or []:
		if row.document_type == doctype:
			return row
	return None


def actions_enabled():
	s = get_settings()
	return bool(cint(s.allow_actions) and (s.action_rules or []))


def assert_within_limits(user=None):
	"""Raise if the user's question/token/cost allocation or the monthly budget is used up.
	Returns the user's token status (see limits.status)."""
	from fibersoft_ai.ai import limits

	user = user or frappe.session.user
	s = get_settings()
	limit = limits.question_limit(user, s)
	if limit:
		today = getdate(nowdate())
		used = frappe.db.count(
			"Fibersoft AI Message",
			{"user": user, "role": "user", "creation": [">=", today]},
		)
		if used >= limit:
			frappe.throw(
				_("You have reached today's limit of {0} questions. Please try again tomorrow.").format(limit),
				title=_("Daily limit reached"),
			)
	budget = flt(s.monthly_budget_usd)
	if budget and month_cost() >= budget:
		frappe.throw(
			_("The assistant's monthly budget has been used. Please contact your System Manager."),
			title=_("Monthly budget reached"),
		)
	return limits.assert_allowed(user)


def month_cost():
	first = get_first_day(nowdate())
	return flt(
		frappe.db.sql(
			"select sum(cost_usd) from `tabFibersoft AI Message` where creation >= %s", (first,)
		)[0][0]
	)


def estimate_cost(usage):
	s = get_settings()
	in_rate = flt(s.input_cost_per_million) / 1_000_000
	out_rate = flt(s.output_cost_per_million) / 1_000_000
	return round(
		cint(usage.get("input_tokens")) * in_rate
		+ cint(usage.get("cache_creation_input_tokens")) * in_rate * 1.25
		+ cint(usage.get("cache_read_input_tokens")) * in_rate * 0.1
		+ cint(usage.get("output_tokens")) * out_rate,
		6,
	)
