"""Per-user token / question / cost allocation (Fibersoft AI Token Limit).

Resolution order for a user:
  1. an enabled "User" rule for that user
  2. the enabled "Role" rule with the highest priority among the user's roles
  3. the enabled "Everyone" rule
  4. no rule -> unlimited tokens (global settings still apply)

Counted tokens = input + output + cache writes + 10% of cached reads, which tracks real cost
(cached reads are billed at one tenth) so long conversations aren't over-penalised.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, format_date, get_first_day, getdate, nowdate

DOCTYPE = "Fibersoft AI Token Limit"
PERIODS = ("daily", "weekly", "monthly")


def billable_tokens(usage):
	return int(
		cint(usage.get("input_tokens"))
		+ cint(usage.get("output_tokens"))
		+ cint(usage.get("cache_creation_input_tokens"))
		+ round(cint(usage.get("cache_read_input_tokens")) * 0.1)
	)


def period_starts(today=None):
	today = getdate(today or nowdate())
	return {
		"daily": today,
		"weekly": add_days(today, -today.weekday()),  # Monday
		"monthly": get_first_day(today),
	}


def reset_label(period, today=None):
	today = getdate(today or nowdate())
	if period == "daily":
		return _("tomorrow")
	if period == "weekly":
		return _("on Monday {0}").format(format_date(add_days(today, 7 - today.weekday())))
	return _("on {0}").format(format_date(add_days(get_first_day(today), 32).replace(day=1)))


def _rule_fields():
	return ["name", "title", "limit_for", "user", "role", "priority", "model", "daily_token_limit",
			"weekly_token_limit", "monthly_token_limit", "daily_question_limit", "monthly_cost_limit_usd", "modified"]


def effective_rule(user=None):
	"""The rule that governs this user (dict) or None."""
	user = user or frappe.session.user
	if not frappe.db.table_exists(DOCTYPE):
		return None
	rules = frappe.get_all(DOCTYPE, filters={"enabled": 1}, fields=_rule_fields(), order_by="modified desc")
	if not rules:
		return None
	for r in rules:
		if r.limit_for == "User" and r.user == user:
			r["source"] = _("User rule")
			return r
	roles = set(frappe.get_roles(user))
	role_rules = [r for r in rules if r.limit_for == "Role" and r.role in roles]
	if role_rules:
		r = sorted(role_rules, key=lambda x: (cint(x.priority), str(x.modified)), reverse=True)[0]
		r["source"] = _("Role rule ({0})").format(r.role)
		return r
	for r in rules:
		if r.limit_for == "Everyone":
			r["source"] = _("Everyone rule")
			return r
	return None


def usage(user=None, today=None):
	"""Counted tokens, questions and cost for the current day / week / month."""
	user = user or frappe.session.user
	starts = period_starts(today)
	out = {}
	for period, start in starts.items():
		row = frappe.db.sql(
			"""select coalesce(sum(billable_tokens), 0), coalesce(sum(role = 'user'), 0), coalesce(sum(cost_usd), 0)
			from `tabFibersoft AI Message` where user = %s and creation >= %s""",
			(user, start),
		)[0]
		out[period] = {"tokens": cint(row[0]), "questions": cint(row[1]), "cost": flt(row[2], 6)}
	return out


def status(user=None):
	"""Limits, usage and remaining tokens for the widget / forms."""
	user = user or frappe.session.user
	rule = effective_rule(user)
	used = usage(user)
	periods = {}
	remaining = None
	for p in PERIODS:
		limit = cint(rule.get(f"{p}_token_limit")) if rule else 0
		entry = {"limit": limit, "used": used[p]["tokens"], "questions": used[p]["questions"], "cost": used[p]["cost"]}
		if limit:
			entry["remaining"] = max(limit - used[p]["tokens"], 0)
			entry["percent"] = min(round(100 * used[p]["tokens"] / limit, 1), 999)
			entry["resets"] = reset_label(p)
			remaining = entry["remaining"] if remaining is None else min(remaining, entry["remaining"])
		periods[p] = entry
	return {
		"user": user,
		"rule": rule.name if rule else None,
		"rule_title": rule.title if rule else None,
		"source": rule.source if rule else _("No limit rule"),
		"model": (rule.model or "").strip() if rule else "",
		"periods": periods,
		"remaining_tokens": remaining,  # None = unlimited
		"has_limits": remaining is not None,
	}


def assert_allowed(user=None):
	"""Raise a friendly error if any allocation for this user is used up. Returns status()."""
	user = user or frappe.session.user
	st = status(user)
	rule = effective_rule(user)
	labels = {"daily": _("daily"), "weekly": _("weekly"), "monthly": _("monthly")}
	for p in PERIODS:
		e = st["periods"][p]
		if e["limit"] and e["used"] >= e["limit"]:
			frappe.throw(
				_("You have used your {0} allocation of {1} tokens for Fibersoft AI. It resets {2}. Ask your System Manager if you need more.").format(
					labels[p], f"{e['limit']:,}", e["resets"]
				),
				title=_("Token limit reached"),
			)
	if rule and flt(rule.monthly_cost_limit_usd) and st["periods"]["monthly"]["cost"] >= flt(rule.monthly_cost_limit_usd):
		frappe.throw(
			_("You have reached your monthly Fibersoft AI cost allocation. It resets {0}.").format(reset_label("monthly")),
			title=_("Limit reached"),
		)
	return st


def question_limit(user, settings):
	rule = effective_rule(user)
	if rule and cint(rule.daily_question_limit):
		return cint(rule.daily_question_limit)
	return cint(settings.daily_message_limit)
