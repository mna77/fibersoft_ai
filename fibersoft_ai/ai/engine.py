"""The agent loop: question -> Claude (with tools) -> tool calls -> ... -> answer, streamed to the browser."""

import json
import time

import frappe
from frappe.utils import cint, now_datetime

from fibersoft_ai.ai import limits, prompt
from fibersoft_ai.ai.claude_client import ClaudeAPIError, ClaudeClient, StreamCancelled
from fibersoft_ai.ai.settings import actions_enabled, estimate_cost, get_api_key, get_settings
from fibersoft_ai.ai.tools import ToolContext, describe_step, execute_tool, get_tool_specs

REALTIME_EVENT = "fibersoft_ai"
STATE_TTL = 900
PUBLISH_INTERVAL = 0.25


def state_key(turn_id):
	return f"fibersoft_ai:turn:{turn_id}"


def cancel_key(turn_id):
	return f"fibersoft_ai:cancel:{turn_id}"


def running_key(user):
	return f"fibersoft_ai:running:{user}"


def get_state(turn_id):
	return frappe.cache.get_value(state_key(turn_id), expires=True)


def set_state(turn_id, state):
	frappe.cache.set_value(state_key(turn_id), state, expires_in_sec=STATE_TTL)


class Turn:
	def __init__(self, conversation, turn_id, user_text, context, user, client=None):
		self.conversation = conversation
		self.turn_id = turn_id
		self.user_text = user_text
		self.context = context or {}
		self.user = user
		self.settings = get_settings()
		self.client = client
		self.state = {
			"turn_id": turn_id, "user": user, "conversation": conversation, "status": "running",
			"text": "", "steps": [], "actions": [], "error": None, "message": None,
		}
		self._last_publish = 0
		self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
		self.trace = []
		self.memo = []
		self.limit_status = limits.status(user)
		self.model = self.limit_status.get("model") or self.settings.model
		self.budget_hit = False

	# ---------------------------------------------------------------- events
	def publish(self, event, force=False, **data):
		set_state(self.turn_id, self.state)
		now = time.time()
		if not force and event == "delta" and now - self._last_publish < PUBLISH_INTERVAL:
			return
		self._last_publish = now
		payload = {"turn_id": self.turn_id, "type": event, "conversation": self.conversation}
		if event == "delta":
			payload["text"] = self.state["text"]
		payload.update(data)
		try:
			frappe.publish_realtime(REALTIME_EVENT, payload, user=self.user, after_commit=False)
		except Exception:
			pass  # polling fallback picks it up from cache

	def on_text(self, chunk):
		self.state["text"] += chunk
		self.publish("delta")

	def cancelled(self):
		return bool(frappe.cache.get_value(cancel_key(self.turn_id), expires=True))

	# ---------------------------------------------------------------- history
	def history(self):
		limit = cint(self.settings.history_messages)
		if not limit:
			return []
		rows = frappe.get_all(
			"Fibersoft AI Message",
			filters={"conversation": self.conversation, "turn_id": ["!=", self.turn_id]},
			fields=["role", "content", "memo", "status"],
			order_by="creation desc",
			limit_page_length=limit,
		)[::-1]
		msgs = []
		for r in rows:
			if r.role == "assistant":
				if r.status == "Error" or not (r.content or "").strip():
					continue
				text = r.content.strip()
				if r.memo:
					text += "\n\n[Data sources used for that answer: " + r.memo[:1500] + "]"
				role = "assistant"
			elif r.role == "event":
				role, text = "user", f"[System event, not typed by the user: {r.content}]"
			else:
				role, text = "user", (r.content or "").strip()
			if not text:
				continue
			if msgs and msgs[-1]["role"] == role:
				msgs[-1]["content"] += "\n\n" + text
			else:
				msgs.append({"role": role, "content": text})
		while msgs and msgs[0]["role"] != "user":
			msgs.pop(0)
		if msgs and msgs[-1]["role"] == "user":
			# the current question will follow; keep alternation valid
			msgs.append({"role": "assistant", "content": "(noted)"})
		return msgs

	# ---------------------------------------------------------------- main loop
	def run(self):
		started = time.time()
		s = self.settings
		acts = actions_enabled()
		ctx = ToolContext(
			user=self.user, conversation=self.conversation, turn_id=self.turn_id,
			screen=self.context, actions=[], actions_allowed=acts,
		)
		tools = get_tool_specs(include_actions=acts)
		caching = cint(s.enable_prompt_caching)
		static_block = {"type": "text", "text": prompt.build_static(acts)}
		if caching:
			static_block["cache_control"] = {"type": "ephemeral"}
		system = [static_block, {"type": "text", "text": prompt.build_dynamic(self.context)}]

		messages = self.history() + [{"role": "user", "content": self.user_text}]
		client = self.client or ClaudeClient(
			get_api_key(), base_url=s.api_base_url or "https://api.anthropic.com", timeout=cint(s.request_timeout) or 120
		)
		max_steps = cint(s.max_tool_iterations) or 15
		status, error = "Completed", None
		self.publish("start", force=True)

		try:
			step = 0
			while True:
				force_answer = step >= max_steps or self.budget_hit
				payload = {
					"model": self.model,
					"max_tokens": cint(s.max_tokens) or 4096,
					"system": system,
					"messages": messages,
					"tools": tools,
				}
				if force_answer:
					payload["tool_choice"] = {"type": "none"}
				resp = client.create(payload, on_text=self.on_text, should_cancel=self.cancelled)
				self._add_usage(resp.get("usage") or {})
				remaining = self.limit_status.get("remaining_tokens")
				if remaining is not None and limits.billable_tokens(self.usage) >= remaining and not self.budget_hit:
					# allocation used up mid-answer: let the model answer with what it has, no more tool calls
					self.budget_hit = True
				# the API rejects empty text blocks when they are sent back
				content = [
					b for b in (resp.get("content") or [])
					if not (b.get("type") == "text" and not (b.get("text") or "").strip())
					and not (b.get("type") == "thinking" and not ((b.get("thinking") or "").strip() and b.get("signature")))
				]
				messages.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no output)"}]})
				tool_uses = [b for b in content if b.get("type") == "tool_use"]

				if resp.get("stop_reason") == "tool_use" and tool_uses and not force_answer:
					step += 1
					results = []
					for block in tool_uses:
						if self.cancelled():
							raise StreamCancelled()
						results.append(self._run_tool(block, ctx))
					frappe.db.commit()  # persist proposed actions, release any locks from dry runs
					messages.append({"role": "user", "content": results})
					if self.state["text"] and not self.state["text"].endswith("\n"):
						self.on_text("\n\n")
					continue

				if resp.get("stop_reason") == "max_tokens":
					self.on_text("\n\n_(Answer cut short because it reached the length limit. Ask me to continue.)_")
				if self.budget_hit:
					self.on_text("\n\n_(Your token allocation was reached while answering, so I stopped looking up more data.)_")
				break
		except StreamCancelled:
			status = "Stopped"
			self.state["text"] += "\n\n_(Stopped)_"
		except ClaudeAPIError as e:
			status, error = "Error", e.friendly()
			frappe.log_error(title="Fibersoft AI: Claude API error", message=f"{e.status} {e}")
		except Exception:
			status, error = "Error", "Something went wrong while answering. The error has been logged for your System Manager."
			frappe.log_error(title="Fibersoft AI: turn failed", message=frappe.get_traceback())
			frappe.db.rollback()

		self.state["actions"] = ctx.actions
		msg_name = self._save(status, error, started)
		self.state.update({"status": "error" if status == "Error" else "done", "error": error, "message": msg_name})
		self.publish("done", force=True, state=self.state)
		return self.state

	def _run_tool(self, block, ctx):
		name, args, tid = block.get("name"), block.get("input") or {}, block.get("id")
		label = describe_step(name, args)
		self.state["steps"].append({"tool": name, "label": label, "status": "running"})
		self.publish("step", force=True, steps=self.state["steps"])
		frappe.set_user(self.user)
		text, is_error, ms = execute_tool(name, args, ctx)
		self.state["steps"][-1].update({"status": "error" if is_error else "done", "ms": ms})
		if ctx.actions:
			self.state["actions"] = ctx.actions
		self.publish("step", force=True, steps=self.state["steps"], actions=self.state["actions"])
		self.trace.append({"tool": name, "input": args, "ms": ms, "error": is_error, "result_preview": text[:1500]})
		args_short = json.dumps(args, default=str, ensure_ascii=False)[:300]
		self.memo.append(f"{name}({args_short}){' -> ERROR' if is_error else ''}")
		return {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": bool(is_error)}

	def _add_usage(self, usage):
		for k in self.usage:
			self.usage[k] += cint(usage.get(k))

	def _save(self, status, error, started):
		cost = estimate_cost(self.usage)
		billable = limits.billable_tokens(self.usage)
		text = self.state["text"].strip()
		msg = frappe.get_doc({
			"doctype": "Fibersoft AI Message",
			"conversation": self.conversation,
			"role": "assistant",
			"user": self.user,
			"status": status,
			"turn_id": self.turn_id,
			"content": text,
			"memo": "; ".join(self.memo)[:6000] or None,
			"error": error,
			"model": self.model,
			"duration_ms": int((time.time() - started) * 1000),
			"context": None,
			"tool_trace": json.dumps(self.trace, default=str, ensure_ascii=False)[:200000] if self.trace else None,
			"input_tokens": self.usage["input_tokens"],
			"output_tokens": self.usage["output_tokens"],
			"cache_read_tokens": self.usage["cache_read_input_tokens"],
			"cache_write_tokens": self.usage["cache_creation_input_tokens"],
			"billable_tokens": billable,
			"cost_usd": cost,
		})
		msg.flags.ignore_permissions = True
		msg.insert()
		conv = frappe.get_doc("Fibersoft AI Conversation", self.conversation)
		conv.db_set({
			"last_message_on": now_datetime(),
			"message_count": cint(conv.message_count) + 1,
			"input_tokens": cint(conv.input_tokens) + self.usage["input_tokens"] + self.usage["cache_read_input_tokens"] + self.usage["cache_creation_input_tokens"],
			"output_tokens": cint(conv.output_tokens) + self.usage["output_tokens"],
			"cost_usd": (conv.cost_usd or 0) + cost,
		}, update_modified=True)
		frappe.db.commit()
		self.state["usage"] = dict(self.usage, cost_usd=cost, billable_tokens=billable)
		return msg.name


def run_turn(conversation, turn_id, user_text, context=None, user=None):
	"""Background job entry point."""
	user = user or frappe.session.user
	frappe.set_user(user)
	try:
		return Turn(conversation, turn_id, user_text, context, user).run()
	finally:
		frappe.cache.delete_value(cancel_key(turn_id))
		if frappe.cache.get_value(running_key(user), expires=True) == turn_id:
			frappe.cache.delete_value(running_key(user))
