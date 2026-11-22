"""Whitelisted endpoints used by the chat widget and the settings page."""

import json
import time

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_first_day, getdate, nowdate

from fibersoft_ai.ai import engine
from fibersoft_ai.ai.settings import assert_within_limits, get_settings, user_is_allowed

MAX_MESSAGE_CHARS = 8000
MAX_CONTEXT_CHARS = 60000


def _require_access():
	if not user_is_allowed():
		frappe.throw(_("You do not have access to Fibersoft AI."), frappe.PermissionError)


def _own_conversation(name):
	row = frappe.db.get_value("Fibersoft AI Conversation", name, ["user", "deleted"], as_dict=True) if name else None
	if not row or row.deleted:
		frappe.throw(_("Conversation not found"), frappe.DoesNotExistError)
	if row.user != frappe.session.user:
		frappe.throw(_("Not your conversation"), frappe.PermissionError)
	return name


def _parse_context(context):
	if not context:
		return {}
	if isinstance(context, str):
		if len(context) > MAX_CONTEXT_CHARS * 2:
			context = context[: MAX_CONTEXT_CHARS * 2]
		try:
			context = json.loads(context)
		except ValueError:
			return {}
	if not isinstance(context, dict):
		return {}
	# trim oversized parts instead of failing
	text = json.dumps(context, default=str)
	if len(text) > MAX_CONTEXT_CHARS:
		for key in ("rows", "doc", "cards", "visible_rows"):
			if key in context:
				context[key] = "(too large - omitted; use tools to read it)"
				if len(json.dumps(context, default=str)) <= MAX_CONTEXT_CHARS:
					break
	return context


@frappe.whitelist()
def send_message(message, conversation=None, context=None):
	_require_access()
	message = (message or "").strip()
	if not message:
		frappe.throw(_("Please type a question"))
	if len(message) > MAX_MESSAGE_CHARS:
		frappe.throw(_("Message is too long (max {0} characters)").format(MAX_MESSAGE_CHARS))
	running = frappe.cache.get_value(engine.running_key(frappe.session.user), expires=True)
	if running:
		st = engine.get_state(running)
		stale = st and st.get("status") == "queued" and time.time() - (st.get("queued_at") or 0) > 180
		if st and st.get("status") in ("queued", "running") and not stale:
			frappe.throw(_("Please wait for the current answer to finish (or press Stop)."), title=_("Busy"))
	assert_within_limits()
	ctx = _parse_context(context)

	if conversation:
		_own_conversation(conversation)
	else:
		conv = frappe.get_doc({
			"doctype": "Fibersoft AI Conversation",
			"title": _make_title(message),
			"user": frappe.session.user,
			"last_message_on": frappe.utils.now_datetime(),
		})
		conv.flags.ignore_permissions = True
		conv.insert()
		conversation = conv.name

	turn_id = frappe.generate_hash(length=16)
	user_msg = frappe.get_doc({
		"doctype": "Fibersoft AI Message",
		"conversation": conversation,
		"role": "user",
		"user": frappe.session.user,
		"content": message,
		"turn_id": turn_id,
		"context": json.dumps(ctx, default=str, ensure_ascii=False)[:MAX_CONTEXT_CHARS] if ctx else None,
	})
	user_msg.flags.ignore_permissions = True
	user_msg.insert()
	frappe.db.set_value("Fibersoft AI Conversation", conversation, "last_message_on", frappe.utils.now_datetime())

	state = {"turn_id": turn_id, "user": frappe.session.user, "conversation": conversation, "status": "queued",
			 "text": "", "steps": [], "actions": [], "error": None, "queued_at": time.time()}
	engine.set_state(turn_id, state)
	frappe.cache.set_value(engine.running_key(frappe.session.user), turn_id, expires_in_sec=900)
	frappe.db.commit()

	if cint(get_settings().run_in_background):
		frappe.enqueue(
			"fibersoft_ai.ai.engine.run_turn",
			queue="short",
			timeout=900,
			job_id=f"fibersoft_ai::{turn_id}",
			conversation=conversation,
			turn_id=turn_id,
			user_text=message,
			context=ctx,
			user=frappe.session.user,
			now=frappe.flags.in_test,
		)
		return {"conversation": conversation, "turn_id": turn_id, "mode": "background"}

	result = engine.run_turn(conversation, turn_id, message, ctx, frappe.session.user)
	return {"conversation": conversation, "turn_id": turn_id, "mode": "sync", "state": result}


def _make_title(message):
	title = " ".join(message.split())
	return (title[:77] + "...") if len(title) > 80 else title


@frappe.whitelist()
def get_turn(turn_id):
	_require_access()
	state = engine.get_state(turn_id)
	if not state:
		# finished long ago: rebuild from DB
		msg = frappe.db.get_value(
			"Fibersoft AI Message", {"turn_id": turn_id, "role": "assistant"},
			["name", "content", "status", "error", "conversation"], as_dict=True,
		)
		if not msg:
			return {"turn_id": turn_id, "status": "unknown"}
		_own_conversation(msg.conversation)
		return {"turn_id": turn_id, "status": "error" if msg.status == "Error" else "done", "text": msg.content,
				"error": msg.error, "message": msg.name, "steps": [], "actions": _turn_actions(turn_id)}
	if state.get("user") != frappe.session.user:
		frappe.throw(_("Not your request"), frappe.PermissionError)
	return state


@frappe.whitelist()
def cancel_turn(turn_id):
	_require_access()
	state = engine.get_state(turn_id)
	if state and state.get("user") == frappe.session.user:
		frappe.cache.set_value(engine.cancel_key(turn_id), 1, expires_in_sec=900)
	return True


def _turn_actions(turn_id):
	from fibersoft_ai.ai.tools.actions import action_view

	return [
		action_view(frappe.get_doc("Fibersoft AI Action", n))
		for n in frappe.get_all("Fibersoft AI Action", filters={"turn_id": turn_id, "user": frappe.session.user},
								pluck="name", order_by="creation asc")
	]


@frappe.whitelist()
def list_conversations(search=None, limit=40):
	_require_access()
	filters = {"user": frappe.session.user, "deleted": 0}
	if search:
		filters["title"] = ["like", f"%{search}%"]
	return frappe.get_all(
		"Fibersoft AI Conversation",
		filters=filters,
		fields=["name", "title", "last_message_on", "message_count"],
		order_by="last_message_on desc",
		limit_page_length=min(cint(limit) or 40, 200),
	)


@frappe.whitelist()
def get_conversation(conversation):
	_require_access()
	_own_conversation(conversation)
	from fibersoft_ai.ai.tools.actions import action_view

	msgs = frappe.get_all(
		"Fibersoft AI Message",
		filters={"conversation": conversation},
		fields=["name", "role", "content", "status", "error", "turn_id", "feedback", "creation", "duration_ms", "cost_usd"],
		order_by="creation asc",
		limit_page_length=500,
	)
	actions = {}
	for a in frappe.get_all("Fibersoft AI Action", filters={"conversation": conversation}, pluck="name", order_by="creation asc"):
		v = action_view(frappe.get_doc("Fibersoft AI Action", a))
		doc_turn = frappe.db.get_value("Fibersoft AI Action", a, "turn_id")
		actions.setdefault(doc_turn, []).append(v)
	for m in msgs:
		if m.role == "assistant":
			m["actions"] = actions.get(m.turn_id, [])
	title = frappe.db.get_value("Fibersoft AI Conversation", conversation, "title")
	return {"name": conversation, "title": title, "messages": msgs}


@frappe.whitelist()
def rename_conversation(conversation, title):
	_require_access()
	_own_conversation(conversation)
	title = (title or "").strip()[:140]
	if title:
		frappe.db.set_value("Fibersoft AI Conversation", conversation, "title", title)
	return title


@frappe.whitelist()
def delete_conversation(conversation):
	_require_access()
	_own_conversation(conversation)
	# soft delete: hidden from the user, but usage stays counted (deleting chats must not reset token limits)
	frappe.db.set_value("Fibersoft AI Conversation", conversation, "deleted", 1)
	frappe.db.set_value("Fibersoft AI Action", {"conversation": conversation, "status": "Pending"}, "status", "Rejected")
	return True


@frappe.whitelist()
def set_feedback(message, feedback, note=None):
	_require_access()
	if feedback not in ("Good", "Bad", ""):
		frappe.throw(_("Invalid feedback"))
	conv = frappe.db.get_value("Fibersoft AI Message", message, "conversation")
	_own_conversation(conv)
	frappe.db.set_value("Fibersoft AI Message", message, {"feedback": feedback, "feedback_note": (note or "")[:1000]},
						update_modified=False)
	return True


def _log_event(action_view):
	"""Record an executed/undone action in the conversation so the assistant knows about it next turn."""
	conv = frappe.db.get_value("Fibersoft AI Action", action_view["name"], "conversation")
	if not conv:
		return
	ev = frappe.get_doc({
		"doctype": "Fibersoft AI Message",
		"conversation": conv,
		"role": "event",
		"user": frappe.session.user,
		"content": f"Action {action_view['action_type']} on {action_view['reference_doctype']} "
				   f"{action_view.get('reference_name') or ''}: {action_view['status']}. {action_view.get('result') or ''}",
	})
	ev.flags.ignore_permissions = True
	ev.insert()


@frappe.whitelist(methods=["POST"])
def execute_action(action):
	_require_access()
	from fibersoft_ai.ai.tools import actions as act

	view = act.execute(action)
	_log_event(view)
	return view


@frappe.whitelist(methods=["POST"])
def reject_action(action):
	_require_access()
	from fibersoft_ai.ai.tools import actions as act

	view = act.reject(action)
	_log_event(view)
	return view


@frappe.whitelist(methods=["POST"])
def undo_action(action):
	_require_access()
	from fibersoft_ai.ai.tools import actions as act

	view = act.undo(action)
	_log_event(view)
	return view


@frappe.whitelist()
def get_my_usage():
	_require_access()
	from fibersoft_ai.ai import limits

	return limits.status()


@frappe.whitelist()
def get_user_usage(user=None, rule=None):
	"""Usage for the Token Limit form: one user, or every user a Role/Everyone rule currently governs."""
	frappe.only_for("System Manager")
	from fibersoft_ai.ai import limits

	if user:
		return {"users": [dict(limits.status(user), full_name=frappe.db.get_value("User", user, "full_name"))]}
	if not rule:
		return {"users": []}
	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]},
						   fields=["name", "full_name"], limit_page_length=500)
	out = []
	for u in users:
		st = limits.status(u.name)
		if st.get("rule") == rule:
			out.append(dict(st, full_name=u.full_name))
	out.sort(key=lambda x: -x["periods"]["monthly"]["used"])
	return {"users": out[:50], "total_users": len(out)}


@frappe.whitelist(methods=["POST"])
def copy_limit_to_users(rule, users):
	"""Create one User rule per selected user, copying the allocation of an existing rule."""
	frappe.only_for("System Manager")
	if isinstance(users, str):
		users = json.loads(users) if users.startswith("[") else [u.strip() for u in users.split(",") if u.strip()]
	src = frappe.get_doc("Fibersoft AI Token Limit", rule)
	created = skipped = 0
	for user in users:
		if not frappe.db.exists("User", user) or frappe.db.exists("Fibersoft AI Token Limit", {"limit_for": "User", "user": user}):
			skipped += 1
			continue
		frappe.get_doc({
			"doctype": "Fibersoft AI Token Limit", "limit_for": "User", "user": user, "enabled": src.enabled,
			"daily_token_limit": src.daily_token_limit, "weekly_token_limit": src.weekly_token_limit,
			"monthly_token_limit": src.monthly_token_limit, "daily_question_limit": src.daily_question_limit,
			"monthly_cost_limit_usd": src.monthly_cost_limit_usd, "model": src.model,
			"note": _("Copied from {0}").format(src.title),
		}).insert()
		created += 1
	return {"created": created, "skipped": skipped}


@frappe.whitelist()
def get_config():
	from fibersoft_ai.boot import widget_config

	return widget_config()


# ------------------------------------------------------------------ admin
@frappe.whitelist()
def test_connection():
	frappe.only_for("System Manager")
	from fibersoft_ai.ai.claude_client import ClaudeAPIError, ClaudeClient
	from fibersoft_ai.ai.settings import clear_local_cache, get_api_key

	clear_local_cache()
	s = get_settings()
	try:
		client = ClaudeClient(get_api_key(), base_url=s.api_base_url or "https://api.anthropic.com", timeout=60, max_retries=1)
		resp = client.create({
			"model": s.model,
			"max_tokens": 30,
			"messages": [{"role": "user", "content": "Reply with exactly: Fibersoft AI connected"}],
		})
		text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
		return {"ok": True, "message": f"Model {resp.get('model', s.model)} replied: {text.strip()}"}
	except ClaudeAPIError as e:
		return {"ok": False, "message": e.friendly() + (f" ({e})" if e.status not in (401,) else "")}
	except Exception as e:
		return {"ok": False, "message": str(e)}


@frappe.whitelist()
def get_usage_stats():
	frappe.only_for("System Manager")
	first = get_first_day(nowdate())
	today = getdate(nowdate())
	q = frappe.db.sql
	questions = q("""select count(*) from `tabFibersoft AI Message` where role='user' and creation >= %s""", first)[0][0]
	questions_today = q("""select count(*) from `tabFibersoft AI Message` where role='user' and creation >= %s and creation < %s""",
						(today, add_days(today, 1)))[0][0]
	cost, tokens = q("""select coalesce(sum(cost_usd), 0), coalesce(sum(billable_tokens), 0) from `tabFibersoft AI Message` where creation >= %s""", first)[0]
	users = q("""select count(distinct user) from `tabFibersoft AI Message` where role='user' and creation >= %s""", first)[0][0]
	good = q("""select count(*) from `tabFibersoft AI Message` where feedback='Good' and creation >= %s""", first)[0][0]
	bad = q("""select count(*) from `tabFibersoft AI Message` where feedback='Bad' and creation >= %s""", first)[0][0]
	top = q("""select user, sum(role='user') as questions, sum(cost_usd) as cost, sum(billable_tokens) as tokens
			   from `tabFibersoft AI Message` where creation >= %s and user is not null
			   group by user order by tokens desc limit 10""", first, as_dict=True)
	executed = frappe.db.count("Fibersoft AI Action", {"status": "Executed", "creation": [">=", first]})
	pending = frappe.db.count("Fibersoft AI Action", {"status": "Pending"})
	return {
		"questions": questions, "questions_today": questions_today, "cost": flt(cost), "tokens": cint(tokens), "users": users,
		"good": good, "bad": bad,
		"top_users": [{"user": t.user, "questions": cint(t.questions), "cost": flt(t.cost), "tokens": cint(t.tokens)} for t in top],
		"actions_executed": executed, "actions_pending": pending, "budget": flt(get_settings().monthly_budget_usd),
	}
