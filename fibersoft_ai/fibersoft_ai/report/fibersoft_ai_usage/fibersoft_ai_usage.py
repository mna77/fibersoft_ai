# Copyright (c) 2026, Fibersoft and contributors
# Fibersoft AI Usage - tokens, questions and cost per user against their allocation.

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate

from fibersoft_ai.ai import limits

PERIOD_KEY = {"Today": "daily", "This Week": "weekly", "This Month": "monthly"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	start, end = date_range(filters)
	data = get_data(filters, start, end)
	return columns(filters), data, None, chart(data), summary(data, start, end)


def date_range(filters):
	period = filters.get("period") or "This Month"
	starts = limits.period_starts()
	if period in PERIOD_KEY:
		return starts[PERIOD_KEY[period]], add_days(getdate(nowdate()), 1)
	if not (filters.get("from_date") and filters.get("to_date")):
		frappe.throw(_("From Date and To Date are required for a custom period"))
	return getdate(filters.from_date), add_days(getdate(filters.to_date), 1)


def columns(filters):
	limit_label = {"Today": _("Daily Limit"), "This Week": _("Weekly Limit"), "This Month": _("Monthly Limit")}.get(
		filters.get("period") or "This Month", _("Limit")
	)
	return [
		{"fieldname": "user", "label": _("User"), "fieldtype": "Link", "options": "User", "width": 200},
		{"fieldname": "full_name", "label": _("Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "rule_title", "label": _("Limit Rule"), "fieldtype": "Data", "width": 160},
		{"fieldname": "questions", "label": _("Questions"), "fieldtype": "Int", "width": 90},
		{"fieldname": "tokens", "label": _("Counted Tokens"), "fieldtype": "Int", "width": 120},
		{"fieldname": "token_limit", "label": limit_label, "fieldtype": "Int", "width": 110},
		{"fieldname": "used_pct", "label": _("Used %"), "fieldtype": "Percent", "width": 85},
		{"fieldname": "avg_tokens", "label": _("Avg Tokens / Question"), "fieldtype": "Int", "width": 120},
		{"fieldname": "cost", "label": _("Cost (USD)"), "fieldtype": "Float", "precision": 4, "width": 100},
		{"fieldname": "good", "label": _("👍"), "fieldtype": "Int", "width": 55},
		{"fieldname": "bad", "label": _("👎"), "fieldtype": "Int", "width": 55},
		{"fieldname": "actions", "label": _("Actions Done"), "fieldtype": "Int", "width": 95},
		{"fieldname": "last_used", "label": _("Last Used"), "fieldtype": "Datetime", "width": 150},
	]


def get_data(filters, start, end):
	cond, values = "", {"start": start, "end": end}
	if filters.get("user"):
		cond = " and m.user = %(user)s"
		values["user"] = filters.user
	rows = frappe.db.sql(
		f"""select m.user, sum(m.role = 'user') as questions, coalesce(sum(m.billable_tokens), 0) as tokens,
			coalesce(sum(m.cost_usd), 0) as cost, sum(m.feedback = 'Good') as good, sum(m.feedback = 'Bad') as bad,
			max(m.creation) as last_used
		from `tabFibersoft AI Message` m
		where m.creation >= %(start)s and m.creation < %(end)s and m.user is not null {cond}
		group by m.user""",
		values,
		as_dict=True,
	)
	by_user = {r.user: r for r in rows}
	if cint(filters.get("include_idle")):
		for rule in frappe.get_all("Fibersoft AI Token Limit", filters={"enabled": 1, "limit_for": "User"}, pluck="user"):
			if rule and rule not in by_user and (not filters.get("user") or rule == filters.user):
				by_user[rule] = frappe._dict(user=rule, questions=0, tokens=0, cost=0, good=0, bad=0, last_used=None)

	actions = dict(
		frappe.db.sql(
			"""select user, count(*) from `tabFibersoft AI Action` where status = 'Executed'
			and executed_on >= %s and executed_on < %s group by user""",
			(start, end),
		)
	)
	period_key = PERIOD_KEY.get(filters.get("period") or "This Month")
	out = []
	for user, r in by_user.items():
		st = limits.status(user)
		limit = st["periods"][period_key]["limit"] if period_key else 0
		tokens, questions = cint(r.tokens), cint(r.questions)
		out.append({
			"user": user,
			"full_name": frappe.db.get_value("User", user, "full_name"),
			"rule": st.get("rule"),
			"rule_title": st.get("rule_title") or _("No limit"),
			"questions": questions,
			"tokens": tokens,
			"token_limit": limit or None,
			"used_pct": round(100 * tokens / limit, 1) if limit else None,
			"avg_tokens": int(tokens / questions) if questions else 0,
			"cost": flt(r.cost, 4),
			"good": cint(r.good),
			"bad": cint(r.bad),
			"actions": cint(actions.get(user)),
			"last_used": r.last_used,
		})
	out.sort(key=lambda x: -x["tokens"])
	return out


def chart(data):
	top = data[:15]
	if not top:
		return None
	return {
		"data": {"labels": [d["full_name"] or d["user"] for d in top], "datasets": [{"name": _("Counted Tokens"), "values": [d["tokens"] for d in top]}]},
		"type": "bar",
		"colors": ["#111111"],
		"height": 260,
	}


def summary(data, start, end):
	tokens = sum(d["tokens"] for d in data)
	questions = sum(d["questions"] for d in data)
	over = sum(1 for d in data if d["used_pct"] and d["used_pct"] >= 90)
	return [
		{"label": _("Counted Tokens"), "value": f"{tokens:,}", "datatype": "Data", "indicator": "Blue"},
		{"label": _("Questions"), "value": f"{questions:,}", "datatype": "Data", "indicator": "Blue"},
		{"label": _("Active Users"), "value": sum(1 for d in data if d["questions"]), "datatype": "Int", "indicator": "Green"},
		{"label": _("Avg Tokens / Question"), "value": f"{int(tokens / questions) if questions else 0:,}", "datatype": "Data", "indicator": "Grey"},
		{"label": _("Cost (USD)"), "value": round(sum(d["cost"] for d in data), 4), "datatype": "Float", "indicator": "Orange"},
		{"label": _("Users ≥ 90% of limit"), "value": over, "datatype": "Int", "indicator": "Red" if over else "Green"},
	]
