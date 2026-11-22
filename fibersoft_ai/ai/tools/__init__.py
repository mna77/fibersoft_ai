"""Tool registry. Each tool is a plain function taking (ctx, **input) and returning JSON-able data."""

import inspect
import json
import time

import frappe

from fibersoft_ai.ai.guard import ToolError

REGISTRY = {}
MAX_RESULT_CHARS = 60000


class ToolContext(frappe._dict):
	"""Per-turn context handed to every tool: user, conversation, turn_id, screen context, created actions."""


def tool(name, description, properties, required=None, kind="read", label=None):
	def wrap(fn):
		REGISTRY[name] = {
			"fn": fn,
			"kind": kind,
			"label": label or name.replace("_", " ").capitalize(),
			"spec": {
				"name": name,
				"description": description.strip(),
				"input_schema": {
					"type": "object",
					"properties": properties,
					"required": required or [],
				},
			},
		}
		return fn

	return wrap


def _load():
	# importing registers the tools
	from fibersoft_ai.ai.tools import actions, calc, read, reports  # noqa: F401


def get_tool_specs(include_actions=False):
	_load()
	specs = []
	for entry in REGISTRY.values():
		if entry["kind"] == "action" and not include_actions:
			continue
		specs.append(entry["spec"])
	return specs


def describe_step(name, args):
	"""Short human label for the live progress line in the chat."""
	_load()
	args = args or {}
	target = args.get("doctype") or args.get("report_name") or args.get("party") or ""
	name_part = args.get("name") or args.get("text") or ""
	labels = {
		"describe_doctype": "Reading structure of {t}",
		"search_doctypes": "Searching DocTypes & reports",
		"find_records": "Finding {t} '{n}'",
		"get_document": "Opening {t} {n}",
		"list_documents": "Listing {t}",
		"count_documents": "Counting {t}",
		"aggregate": "Totalling {t}",
		"get_document_activity": "Reading history of {t} {n}",
		"get_report_info": "Reading report {t}",
		"run_report": "Running report {t}",
		"get_balance": "Calculating balance {t}",
		"get_stock_levels": "Checking stock",
		"calculate": "Calculating",
	}
	if name.startswith("propose_"):
		return "Preparing action on {t}".format(t=target).strip()
	return labels.get(name, REGISTRY.get(name, {}).get("label", name)).format(t=target, n=name_part).strip()


def execute_tool(name, args, ctx):
	"""Run a tool. Returns (result_text, is_error, elapsed_ms). Never raises for tool-level problems."""
	_load()
	started = time.time()
	entry = REGISTRY.get(name)
	if not entry:
		return json.dumps({"error": f"Unknown tool '{name}'"}), True, 0
	if entry["kind"] == "action" and not ctx.get("actions_allowed"):
		return json.dumps({"error": "Actions are disabled in Fibersoft AI Settings."}), True, 0
	if not isinstance(args, dict) or "__invalid_json__" in args:
		return json.dumps({"error": "Tool input was not valid JSON. Please retry with valid arguments."}), True, 0

	try:
		inspect.signature(entry["fn"]).bind(ctx, **args)
	except TypeError as e:
		props = list(entry["spec"]["input_schema"]["properties"])
		return json.dumps({"error": f"Invalid arguments for {name}: {e}. Accepted: {', '.join(props)}"}), True, 0

	savepoint = "fai_tool"
	frappe.db.savepoint(savepoint)
	try:
		result = entry["fn"](ctx, **args)
		is_error = False
	except ToolError as e:
		frappe.db.rollback(save_point=savepoint)
		result, is_error = {"error": str(e)}, True
	except frappe.PermissionError as e:
		frappe.db.rollback(save_point=savepoint)
		msg = _clean_message(e) or "Permission denied"
		result, is_error = {"error": f"Permission denied for user {frappe.session.user}: {msg}"}, True
	except frappe.DoesNotExistError as e:
		frappe.db.rollback(save_point=savepoint)
		result, is_error = {"error": _clean_message(e) or "Record not found"}, True
	except frappe.ValidationError as e:
		frappe.db.rollback(save_point=savepoint)
		result, is_error = {"error": _clean_message(e) or "Validation error"}, True
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"Fibersoft AI tool {name} failed", message=frappe.get_traceback())
		result, is_error = {"error": f"Internal error in tool {name}: {_clean_message(e) or type(e).__name__}"}, True
	finally:
		frappe.clear_messages()

	text = json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":"))
	if len(text) > MAX_RESULT_CHARS:
		text = (
			text[:MAX_RESULT_CHARS]
			+ f'... [RESULT TRUNCATED at {MAX_RESULT_CHARS} of {len(text)} chars. Narrow the query (filters, fields, limit) for complete data.]'
		)
	return text, is_error, int((time.time() - started) * 1000)


def _clean_message(e):
	from frappe.utils import strip_html

	msg = str(e) or ""
	if not msg and getattr(frappe.local, "message_log", None):
		try:
			last = frappe.local.message_log[-1]
			msg = json.loads(last).get("message") if isinstance(last, str) else last.get("message")
		except Exception:
			msg = ""
	return strip_html(msg or "").strip()[:1500]
