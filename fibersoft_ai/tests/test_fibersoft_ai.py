"""Fibersoft AI test-suite. Run with:  bench --site <site> run-tests --app fibersoft_ai

Uses a scripted fake Claude client, so no API key or network is needed. Accounting / stock tests are skipped
automatically on sites without the accounting/stock modules.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from fibersoft_ai.ai import engine
from fibersoft_ai.ai.claude_client import ClaudeClient, StreamAssembler
from fibersoft_ai.ai.settings import clear_local_cache
from fibersoft_ai.ai.tools import ToolContext, execute_tool, get_tool_specs
from fibersoft_ai.ai.tools import actions as act

TEST_USER = "fai_sales_user@example.com"
OTHER_USER = "fai_other_user@example.com"


def has_accounts():
	return bool(frappe.db.exists("DocType", "Sales Invoice") and frappe.db.get_value("Company", {}, "name"))


def ok(_tool, **args):
	ctx = ToolContext(user=frappe.session.user, conversation=None, turn_id="t-test", screen={}, actions=[], actions_allowed=True)
	text, is_error, _ms = execute_tool(_tool, args, ctx)
	data = json.loads(text)
	assert not is_error, f"{_tool} failed: {data}"
	return data, ctx


def err(_tool, **args):
	ctx = ToolContext(user=frappe.session.user, conversation=None, turn_id="t-test", screen={}, actions=[], actions_allowed=True)
	text, is_error, _ms = execute_tool(_tool, args, ctx)
	assert is_error, f"{_tool} should have failed but returned {text[:300]}"
	return json.loads(text)["error"]


def configure(_blocked=(), _redacted=(), _rules=None, **kw):
	s = frappe.get_single("Fibersoft AI Settings")
	base = dict(enabled=1, model="claude-sonnet-5-5", run_in_background=1, daily_message_limit=0, monthly_budget_usd=0,
				allow_actions=1, validate_before_proposing=1, max_tool_iterations=6, history_messages=20)
	base.update(kw)
	s.update(base)
	s.set("allowed_roles", [])
	s.set("blocked_doctypes", [{"document_type": d} for d in _blocked])
	s.set("redacted_fields", [{"document_type": d, "fieldname": f} for d, f in _redacted])
	rules = [
		{"document_type": "ToDo", "allow_create": 1, "allow_update": 1, "allow_comment": 1, "allow_assign": 1},
		{"document_type": "Note", "allow_create": 0, "allow_update": 0, "allow_comment": 1},
	]
	if has_accounts():
		rules.append({"document_type": "Sales Invoice", "allow_create": 1, "allow_update": 1, "allow_submit": 1,
					  "allow_cancel": 1, "allow_comment": 1})
	s.set("action_rules", _rules if _rules is not None else rules)
	s.api_key = "sk-test-key"
	s.save(ignore_permissions=True)
	frappe.db.delete("Fibersoft AI Token Limit")  # tests start with no allocations unless they create them
	frappe.clear_document_cache("Fibersoft AI Settings", "Fibersoft AI Settings")
	clear_local_cache()


def make_user(email, roles):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
							"user_type": "System User"})
		u.insert(ignore_permissions=True)
	u = frappe.get_doc("User", email)
	u.set("roles", [])
	for r in roles:
		u.append("roles", {"role": r})
	u.save(ignore_permissions=True)
	return email


# ------------------------------------------------------------------ fake Claude
class FakeClient:
	"""Returns scripted responses. Each script item: str (final text) or list of (tool_name, input) tool calls."""

	def __init__(self, script):
		self.script = list(script)
		self.payloads = []

	def create(self, payload, on_text=None, should_cancel=None):
		self.payloads.append(json.loads(json.dumps(payload, default=str)))
		item = self.script.pop(0) if self.script else "Done."
		usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0}
		if isinstance(item, str):
			if on_text:
				for i in range(0, len(item), 7):
					on_text(item[i : i + 7])
			return {"content": [{"type": "text", "text": item}], "stop_reason": "end_turn", "usage": usage}
		blocks = [{"type": "tool_use", "id": f"toolu_{len(self.payloads)}_{i}", "name": n, "input": a} for i, (n, a) in enumerate(item)]
		return {"content": [{"type": "text", "text": ""}] + blocks, "stop_reason": "tool_use", "usage": usage}


class FakeStreamResponse:
	def __init__(self, lines):
		self.lines = lines
		self.status_code = 200

	def iter_lines(self, decode_unicode=True):
		yield from self.lines

	def close(self):
		pass


# ------------------------------------------------------------------ tests
class TestCore(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		configure()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_calculate_exact(self):
		data, _ = ok("calculate", expressions={
			"a": "0.1 + 0.2", "b": "pct(450000, 1800000)", "c": "round(1234.5678, 2)", "d": "pct_change(100, 125)",
			"e": "1,250,000 * 2", "f": "10 / 0",
		})
		r = data["results"]
		self.assertEqual(r["a"], "0.3")
		self.assertEqual(r["b"], "25")
		self.assertEqual(r["c"], "1234.57")
		self.assertEqual(r["d"], "25")
		self.assertEqual(r["e"], "2500000")
		self.assertTrue(r["f"].startswith("ERROR"))
		self.assertIn("ERROR", ok("calculate", expressions={"x": "__import__('os')"})[0]["results"]["x"])

	def test_tool_specs_valid(self):
		specs = get_tool_specs(include_actions=True)
		names = {s["name"] for s in specs}
		for n in ("describe_doctype", "list_documents", "aggregate", "run_report", "calculate", "propose_create_document"):
			self.assertIn(n, names)
		for s in specs:
			self.assertEqual(s["input_schema"]["type"], "object")
			json.dumps(s)
		self.assertNotIn("propose_create_document", {s["name"] for s in get_tool_specs(include_actions=False)})

	def test_describe_and_search(self):
		data, _ = ok("describe_doctype", doctype="ToDo")
		self.assertIn("description", [f["fieldname"] for f in data["fields"]])
		self.assertTrue(data["your_permissions"]["read"])
		self.assertIn("does not exist", err("describe_doctype", doctype="No Such Doctype"))
		data, _ = ok("search_doctypes", text="todo")
		self.assertIn("ToDo", [d["name"] for d in data["doctypes"]])

	def test_hard_blocked_and_sensitive(self):
		self.assertIn("not allowed", err("get_document", doctype="Fibersoft AI Settings", name="Fibersoft AI Settings"))
		self.assertIn("not allowed", err("list_documents", doctype="Fibersoft AI Message"))
		data, _ = ok("get_document", doctype="User", name="Administrator")
		doc = data["document"]
		for f in ("new_password", "api_secret", "api_key", "reset_password_key"):
			self.assertNotIn(f, doc)

	def test_admin_blocked_and_redacted(self):
		configure(_blocked=["Note"], _redacted=[("ToDo", "description")])
		try:
			self.assertIn("not allowed", err("list_documents", doctype="Note"))
			t = frappe.get_doc({"doctype": "ToDo", "description": "secret plan"}).insert()
			data, _ = ok("get_document", doctype="ToDo", name=t.name)
			self.assertNotIn("description", data["document"])
			self.assertIn("not a readable field", err("list_documents", doctype="ToDo", fields=["description"]))
		finally:
			configure()

	def test_list_count_filters(self):
		for i in range(3):
			frappe.get_doc({"doctype": "ToDo", "description": f"fai list test {i}", "priority": "High"}).insert()
		data, _ = ok("list_documents", doctype="ToDo", filters=[["description", "like", "%fai list test%"]], fields=["name", "priority"], limit=2)
		self.assertEqual(data["total_matching"], 3)
		self.assertEqual(data["returned"], 2)
		self.assertIn("more", data)
		data, _ = ok("count_documents", doctype="ToDo", filters={"description": ["like", "%fai list test%"], "priority": "High"})
		self.assertEqual(data["count"], 3)
		e = err("list_documents", doctype="ToDo", filters=[["no_field", "=", 1]])
		self.assertIn("not a readable field", e)
		self.assertIn("Unsupported operator", err("list_documents", doctype="ToDo", filters=[["priority", "~", 1]]))
		self.assertIn("order_by", err("list_documents", doctype="ToDo", order_by="description; drop table x"))
		self.assertIn("not a readable field", err("list_documents", doctype="ToDo", order_by="password desc"))

	def test_aggregate_group_and_period(self):
		frappe.db.delete("ToDo", {"description": ["like", "fai agg%"]})
		rows = [("High", "2026-01-05"), ("High", "2026-01-20"), ("Low", "2026-02-03")]
		for p, d in rows:
			frappe.get_doc({"doctype": "ToDo", "description": "fai agg", "priority": p, "date": d}).insert()
		data, _ = ok("aggregate", doctype="ToDo", metrics=[{"function": "count"}], group_by=["priority"],
					 filters=[["description", "=", "fai agg"]], order_by="value desc")
		self.assertEqual(data["groups"][0], {"priority": "High", "value": 2})
		self.assertEqual(data["grand_total_all_groups"]["value"], 3)
		data, _ = ok("aggregate", doctype="ToDo", metrics=[{"function": "count"}], date_field="date", period="month",
					 filters=[["description", "=", "fai agg"]])
		self.assertEqual([(g["period"], g["value"]) for g in data["groups"]], [("2026-01", 2), ("2026-02", 1)])
		self.assertIn("not numeric", err("aggregate", doctype="ToDo", metrics=[{"function": "sum", "field": "description"}]))

	def test_permission_denied_for_restricted_user(self):
		if not frappe.db.exists("DocType", "GL Entry"):
			self.skipTest("no accounting")
		make_user(TEST_USER, ["Sales User"])
		frappe.set_user(TEST_USER)
		e = err("list_documents", doctype="GL Entry")
		self.assertIn("ermission", e)
		e = err("get_balance", party_type="Customer", party="x")
		self.assertIn("ermission", e)

	def test_other_users_todo_not_visible(self):
		t = frappe.get_doc({"doctype": "ToDo", "description": "admin private todo", "allocated_to": "Administrator"}).insert()
		make_user(OTHER_USER, ["Sales User"])
		frappe.set_user(OTHER_USER)
		e = err("get_document", doctype="ToDo", name=t.name)
		self.assertIn("ermission", e)
		data, _ = ok("count_documents", doctype="ToDo", filters={"name": t.name})
		self.assertEqual(data["count"], 0)

	def test_stream_assembler(self):
		events = [
			("message_start", {"type": "message_start", "message": {"id": "m1", "model": "x", "usage": {"input_tokens": 12, "cache_read_input_tokens": 3}}}),
			("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
			("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}),
			("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}}),
			("content_block_stop", {"type": "content_block_stop", "index": 0}),
			("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "calculate", "input": {}}}),
			("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"expressions": {"a"'}}),
			("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "1+1"}}'}}),
			("content_block_stop", {"type": "content_block_stop", "index": 1}),
			("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}}),
			("message_stop", {"type": "message_stop"}),
		]
		lines = []
		for name, data in events:
			lines += [f"event: {name}", "data: " + json.dumps(data), ""]
		lines.insert(3, ": ping comment")
		client = ClaudeClient("k")
		got = []
		msg = client._consume_stream(FakeStreamResponse(lines), got.append, None, {"any": False})
		self.assertEqual("".join(got), "Hello")
		self.assertEqual(msg["stop_reason"], "tool_use")
		self.assertEqual(msg["content"][1]["input"], {"expressions": {"a": "1+1"}})
		self.assertEqual(msg["usage"], {"input_tokens": 12, "cache_read_input_tokens": 3, "output_tokens": 40})

	def test_stream_error_event(self):
		from fibersoft_ai.ai.claude_client import ClaudeAPIError

		a = StreamAssembler()
		with self.assertRaises(ClaudeAPIError) as cm:
			a.feed("error", json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
		self.assertEqual(cm.exception.status, 529)


class TestActions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		configure()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _propose(self, _tool, **args):
		data, ctx = ok(_tool, **args)
		self.assertEqual(data["status"], "awaiting_user_confirmation")
		self.assertEqual(len(ctx.actions), 1)
		return data["action_id"], data

	def test_create_execute_undo_todo(self):
		before = frappe.db.count("ToDo")
		aid, data = self._propose("propose_create_document", doctype="ToDo",
								  values={"description": "Call Ali Traders", "priority": "High", "date": add_days(nowdate(), 2)},
								  summary="Create a reminder")
		self.assertEqual(frappe.db.count("ToDo"), before, "dry run must not leave a record")
		self.assertEqual(frappe.db.get_value("Fibersoft AI Action", aid, "status"), "Pending")
		view = act.execute(aid)
		self.assertEqual(view["status"], "Executed", view)
		self.assertTrue(frappe.db.exists("ToDo", view["reference_name"]))
		self.assertEqual(frappe.db.get_value("ToDo", view["reference_name"], "priority"), "High")
		with self.assertRaises(frappe.ValidationError):
			act.execute(aid)  # cannot run twice
		view = act.undo(aid)
		self.assertEqual(view["status"], "Undone")
		self.assertFalse(frappe.db.exists("ToDo", view["reference_name"]))

	def test_update_undo_and_conflict(self):
		t = frappe.get_doc({"doctype": "ToDo", "description": "original", "priority": "Low"}).insert()
		aid, data = self._propose("propose_update_document", doctype="ToDo", name=t.name, values={"priority": "High"}, summary="Raise priority")
		self.assertEqual(data["preview"]["changes"]["Priority"], {"from": "Low", "to": "High"})
		self.assertEqual(frappe.db.get_value("ToDo", t.name, "priority"), "Low")
		act.execute(aid)
		self.assertEqual(frappe.db.get_value("ToDo", t.name, "priority"), "High")
		act.undo(aid)
		self.assertEqual(frappe.db.get_value("ToDo", t.name, "priority"), "Low")

		aid2, _ = self._propose("propose_update_document", doctype="ToDo", name=t.name, values={"priority": "Medium"}, summary="x")
		t.reload()
		t.description = "changed by someone else"
		t.save()
		with self.assertRaises(frappe.ValidationError):
			act.execute(aid2)

	def test_validation_errors(self):
		t = frappe.get_doc({"doctype": "ToDo", "description": "v"}).insert()
		self.assertIn("no field", err("propose_update_document", doctype="ToDo", name=t.name, values={"nope": 1}, summary="x"))
		self.assertIn("not a valid option", err("propose_update_document", doctype="ToDo", name=t.name, values={"priority": "Urgent"}, summary="x"))
		self.assertIn("system field", err("propose_update_document", doctype="ToDo", name=t.name, values={"owner": "x"}, summary="x"))
		self.assertIn("does not exist", err("propose_update_document", doctype="ToDo", name=t.name, values={"allocated_to": "ghost@nowhere.com"}, summary="x"))
		self.assertIn("already set", err("propose_update_document", doctype="ToDo", name=t.name, values={"description": "v"}, summary="x"))
		self.assertIn("not enabled", err("propose_create_document", doctype="Note", values={"title": "x"}, summary="x"))
		self.assertIn("cannot be changed", err("propose_create_document", doctype="User", values={"email": "a@b.com"}, summary="x"))
		self.assertIn("not enabled", err("propose_create_document", doctype="Event", values={"subject": "x"}, summary="x"))

	def test_actions_disabled(self):
		ctx = ToolContext(user="Administrator", actions=[], actions_allowed=False)
		text, is_error, _ = execute_tool("propose_create_document", {"doctype": "ToDo", "values": {"description": "x"}, "summary": "x"}, ctx)
		self.assertTrue(is_error)
		self.assertIn("disabled", text)

	def test_comment_and_assign(self):
		t = frappe.get_doc({"doctype": "ToDo", "description": "assign me"}).insert()
		aid, _ = self._propose("propose_comment", doctype="ToDo", name=t.name, comment="Please follow up <b>today</b>")
		view = act.execute(aid)
		self.assertEqual(view["status"], "Executed")
		c = frappe.get_all("Comment", {"reference_doctype": "ToDo", "reference_name": t.name, "comment_type": "Comment"}, pluck="content")
		self.assertTrue(any("&lt;b&gt;" in x for x in c), "comment html must be escaped")
		act.undo(aid)
		self.assertFalse(frappe.get_all("Comment", {"reference_doctype": "ToDo", "reference_name": t.name, "comment_type": "Comment"}))

		aid, _ = self._propose("propose_assignment", doctype="ToDo", name=t.name, assign_to=["Administrator"], description="check")
		act.execute(aid)
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": "ToDo", "reference_name": t.name, "allocated_to": "Administrator", "status": "Open"}))
		act.undo(aid)
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": "ToDo", "reference_name": t.name, "allocated_to": "Administrator", "status": "Open"}))

	def test_other_user_cannot_execute(self):
		aid, _ = self._propose("propose_create_document", doctype="ToDo", values={"description": "mine"}, summary="x")
		make_user(OTHER_USER, ["Sales User"])
		frappe.set_user(OTHER_USER)
		with self.assertRaises(frappe.PermissionError):
			act.execute(aid)

	def test_reject_and_expiry(self):
		aid, _ = self._propose("propose_create_document", doctype="ToDo", values={"description": "reject me"}, summary="x")
		self.assertEqual(act.reject(aid)["status"], "Rejected")
		aid, _ = self._propose("propose_create_document", doctype="ToDo", values={"description": "expire me"}, summary="x")
		frappe.db.set_value("Fibersoft AI Action", aid, "creation", add_days(nowdate(), -3))
		with self.assertRaises(frappe.ValidationError):
			act.execute(aid)
		self.assertEqual(frappe.db.get_value("Fibersoft AI Action", aid, "status"), "Expired")

	def test_workflow_action(self):
		for st in ("FAI Draft", "FAI Approved"):
			if not frappe.db.exists("Workflow State", st):
				frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": st}).insert()
		if not frappe.db.exists("Workflow Action Master", "FAI Approve"):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": "FAI Approve"}).insert()
		if frappe.db.exists("Workflow", "FAI Note Flow"):
			frappe.delete_doc("Workflow", "FAI Note Flow", force=True)
		frappe.get_doc({
			"doctype": "Workflow", "workflow_name": "FAI Note Flow", "document_type": "Note", "is_active": 1,
			"workflow_state_field": "workflow_state", "send_email_alert": 0,
			"states": [{"state": "FAI Draft", "doc_status": "0", "allow_edit": "System Manager"},
					   {"state": "FAI Approved", "doc_status": "0", "allow_edit": "System Manager"}],
			"transitions": [{"state": "FAI Draft", "action": "FAI Approve", "next_state": "FAI Approved", "allowed": "System Manager", "allow_self_approval": 1}],
		}).insert()
		try:
			configure(_rules=[{"document_type": "Note", "allow_workflow": 1}])
			note = frappe.get_doc({"doctype": "Note", "title": "FAI wf " + frappe.generate_hash(length=5)}).insert()
			self.assertEqual(note.workflow_state, "FAI Draft")
			self.assertIn("Available: FAI Approve", err("propose_workflow_action", doctype="Note", name=note.name, action="Reject", summary="x"))
			aid, data = self._propose("propose_workflow_action", doctype="Note", name=note.name, action="fai approve", summary="Approve note")
			self.assertEqual(data["preview"]["to_state"], "FAI Approved")
			self.assertEqual(frappe.db.get_value("Note", note.name, "workflow_state"), "FAI Draft")
			view = act.execute(aid)
			self.assertEqual(view["status"], "Executed", view)
			self.assertEqual(frappe.db.get_value("Note", note.name, "workflow_state"), "FAI Approved")
			data, _ = ok("describe_doctype", doctype="Note")
			self.assertEqual(data["workflow"]["transitions"][0]["action"], "FAI Approve")
		finally:
			frappe.db.set_value("Workflow", "FAI Note Flow", "is_active", 0)
			frappe.cache.delete_value("workflow_" + "Note")
			configure()

	def test_dry_run_isolation(self):
		from fibersoft_ai.ai.dryrun import DryRunCommitAttempt, dry_run

		n = len(frappe.db.after_commit._functions)
		before = frappe.db.count("ToDo")
		with dry_run():
			frappe.get_doc({"doctype": "ToDo", "description": "dry"}).insert()
			frappe.db.after_commit.add(lambda: None)
			self.assertEqual(frappe.db.count("ToDo"), before + 1)
		self.assertEqual(frappe.db.count("ToDo"), before)
		self.assertEqual(len(frappe.db.after_commit._functions), n)
		with self.assertRaises(DryRunCommitAttempt):
			with dry_run():
				frappe.get_doc({"doctype": "ToDo", "description": "dry commit"}).insert()
				frappe.db.commit()
		self.assertFalse(frappe.db.exists("ToDo", {"description": "dry commit"}))


class TestAccounting(FrappeTestCase):
	"""Realistic tests on accounting + stock (skipped when not installed)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not has_accounts():
			return
		frappe.set_user("Administrator")
		configure()
		cls.company = frappe.db.get_value("Company", {}, "name")
		abbr = frappe.db.get_value("Company", cls.company, "abbr")
		cls.wh = f"Stores - {abbr}"
		sfx = frappe.generate_hash(length=4).upper()
		cls.shirt, cls.trouser = f"FAI-SHIRT-{sfx}", f"FAI-TROUSER-{sfx}"
		cls.ali, cls.zain = f"FAI Ali Traders {sfx}", f"FAI Zain Garments {sfx}"
		for code in (cls.shirt, cls.trouser):
			if not frappe.db.exists("Item", code):
				frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "Products",
								"stock_uom": "Nos", "is_stock_item": 1}).insert()
		for c in (cls.ali, cls.zain):
			if not frappe.db.exists("Customer", c):
				frappe.get_doc({"doctype": "Customer", "customer_name": c, "customer_group": "Commercial", "territory": "Rest Of The World"}).insert()
		cls.invoices = []
		for cust, rows, submit in [
			(cls.ali, [(cls.shirt, 10, 100), (cls.shirt, 5, 100), (cls.trouser, 2, 250)], True),
			(cls.ali, [(cls.trouser, 4, 250)], True),
			(cls.zain, [(cls.shirt, 1, 999)], True),
			(cls.zain, [(cls.shirt, 7, 100)], False),
		]:
			si = frappe.get_doc({"doctype": "Sales Invoice", "customer": cust, "company": cls.company,
								 "posting_date": nowdate(), "due_date": nowdate(), "set_posting_time": 1,
								 "items": [{"item_code": i, "qty": q, "rate": r} for i, q, r in rows]})
			si.insert()
			if submit:
				si.submit()
			cls.invoices.append(si.name)
		# one cancelled invoice that must not count
		si = frappe.get_doc({"doctype": "Sales Invoice", "customer": cls.ali, "company": cls.company,
							 "posting_date": nowdate(), "due_date": nowdate(), "items": [{"item_code": cls.shirt, "qty": 100, "rate": 100}]})
		si.insert()
		si.submit()
		si.cancel()
		frappe.db.commit()

	def setUp(self):
		if not has_accounts():
			self.skipTest("accounting not installed")
		frappe.set_user("Administrator")

	def test_aggregate_matches_sql(self):
		data, _ = ok("aggregate", doctype="Sales Invoice", metrics=[{"function": "sum", "field": "grand_total"}, {"function": "count"}],
					 group_by=["customer"], filters=[["customer", "in", [self.ali, self.zain]], ["docstatus", "=", 1]], order_by="value desc")
		expected = frappe.db.sql("""select customer, sum(grand_total) from `tabSales Invoice`
			where customer in %s and docstatus = 1 group by customer""", ((self.ali, self.zain),))
		got = {g["customer"]: g["value"] for g in data["groups"]}
		for cust, total in expected:
			self.assertAlmostEqual(got[cust], flt(total), places=2)
		self.assertEqual(got[self.ali], 3000.0)  # 2000 + 1000 (cancelled 10000 excluded)

	def test_default_excludes_cancelled(self):
		data, _ = ok("count_documents", doctype="Sales Invoice", filters=[["customer", "=", self.ali]])
		self.assertEqual(data["count"], 2)
		self.assertIn("Cancelled", data["note"])
		data, _ = ok("count_documents", doctype="Sales Invoice", filters=[["customer", "=", self.ali]], include_cancelled=True)
		self.assertEqual(data["count"], 3)

	def test_child_filter_no_double_counting(self):
		# invoice 1 has two FAI-SHIRT rows; joining child rows must not double its grand_total
		data, _ = ok("aggregate", doctype="Sales Invoice", metrics=[{"function": "sum", "field": "grand_total"}],
					 filters=[["Sales Invoice Item", "item_code", "=", self.shirt], ["customer", "=", self.ali], ["docstatus", "=", 1]])
		self.assertEqual(data["groups"][0]["value"], 2000.0)

	def test_get_document_and_find(self):
		data, _ = ok("find_records", doctype="Customer", text=self.ali[:12])
		self.assertIn(self.ali, [m["name"] for m in data["matches"]])
		data, _ = ok("get_document", doctype="Sales Invoice", name=self.invoices[0])
		doc = data["document"]
		self.assertEqual(len(doc["items"]), 3)
		self.assertEqual(doc["grand_total"], 2000.0)
		self.assertTrue(doc["url"].startswith("/app/sales-invoice/"))

	def test_balance_and_report(self):
		data, _ = ok("get_balance", party_type="Customer", party=self.ali, company=self.company)
		self.assertEqual(data["closing"]["balance"], 3000.0)
		data, _ = ok("run_report", report_name="Accounts Receivable",
					 filters={"company": self.company, "report_date": nowdate(), "party_type": "Customer"},
					 where=[["party", "in", [self.ali, self.zain]]], sort_by="outstanding", sort_desc=True)
		self.assertEqual(data["column_totals"].get("outstanding"), 3999.0)  # 3000 + 999
		self.assertEqual(data["rows"][0]["party"], self.ali)
		info, _ = ok("get_report_info", report_name="Accounts Receivable")
		self.assertIn("company", [f["fieldname"] for f in info["filters"]])
		self.assertIn("not found", err("run_report", report_name="Accounts Recievable XYZ"))

	def test_describe_si_has_reports_and_children(self):
		data, _ = ok("describe_doctype", doctype="Sales Invoice")
		self.assertIn("items", data["child_tables"])
		self.assertTrue(data["is_submittable"])

	def test_si_create_submit_cancel_cycle(self):
		e = err("propose_create_document", doctype="Sales Invoice", values={"customer": "Nobody Ltd", "items": []}, summary="x")
		self.assertIn("does not exist", e)
		before = frappe.db.count("Sales Invoice")
		aid, data = TestActions._propose(self, "propose_create_document", doctype="Sales Invoice", values={
			"customer": self.zain, "company": self.company, "due_date": nowdate(),
			"items": [{"item_code": self.shirt, "qty": 3, "rate": 120}]}, summary="Invoice Zain 3 shirts")
		self.assertEqual(frappe.db.count("Sales Invoice"), before)
		self.assertEqual(data["preview"]["calculated"]["Grand Total"], 360.0)
		view = act.execute(aid)
		self.assertEqual(view["status"], "Executed", view)
		name = view["reference_name"]
		aid, _ = TestActions._propose(self, "propose_submit_document", doctype="Sales Invoice", name=name, summary="submit")
		self.assertEqual(frappe.db.get_value("Sales Invoice", name, "docstatus"), 0)
		act.execute(aid)
		self.assertEqual(frappe.db.get_value("Sales Invoice", name, "docstatus"), 1)
		aid, _ = TestActions._propose(self, "propose_cancel_document", doctype="Sales Invoice", name=name, summary="cancel")
		act.execute(aid)
		self.assertEqual(frappe.db.get_value("Sales Invoice", name, "docstatus"), 2)

	def test_si_dry_run_surfaces_real_validation(self):
		e = err("propose_create_document", doctype="Sales Invoice",
				values={"customer": self.zain, "company": self.company, "due_date": add_days(nowdate(), -30),
						"posting_date": nowdate(), "set_posting_time": 1, "items": [{"item_code": self.shirt, "qty": 1, "rate": 1}]},
				summary="x")
		self.assertIn("Dry run failed", e)

	def test_stock_levels(self):
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.company,
							 "items": [{"item_code": self.trouser, "qty": 12, "t_warehouse": self.wh, "basic_rate": 50}]})
		se.insert()
		se.submit()
		data, _ = ok("get_stock_levels", item_code=self.trouser)
		self.assertEqual(data["total_actual_qty"], 12.0)


class TestEngineAndApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		configure()

	def tearDown(self):
		frappe.set_user("Administrator")
		configure()

	def _send(self, script, message="How many open todos?", context=None, conversation=None):
		fake = FakeClient(script)
		with patch("fibersoft_ai.ai.engine.ClaudeClient", lambda *a, **k: fake):
			from fibersoft_ai import api

			r = api.send_message(message, conversation=conversation, context=json.dumps(context or {"route": ["List", "ToDo"], "doctype": "ToDo"}))
		return r, fake

	def test_full_turn_with_tool(self):
		desc = "engine test " + frappe.generate_hash(length=6)
		frappe.get_doc({"doctype": "ToDo", "description": desc, "status": "Open"}).insert()
		r, fake = self._send([[("count_documents", {"doctype": "ToDo", "filters": {"description": desc}})], "You have **1** open ToDo."])
		from fibersoft_ai import api

		state = api.get_turn(r["turn_id"])
		self.assertEqual(state["status"], "done", state)
		self.assertIn("You have **1** open ToDo.", state["text"])
		self.assertEqual(state["steps"][0]["status"], "done")
		# second request carried the tool result back
		tool_result = fake.payloads[1]["messages"][-1]["content"][0]
		self.assertEqual(tool_result["type"], "tool_result")
		self.assertEqual(json.loads(tool_result["content"])["count"], 1)
		# empty text block was stripped before sending back
		self.assertTrue(all(b.get("text") != "" for b in fake.payloads[1]["messages"][-2]["content"]))
		# system prompt has cached static part + screen context
		sys = fake.payloads[0]["system"]
		self.assertIn("cache_control", sys[0])
		self.assertIn("<current_screen>", sys[1]["text"])
		self.assertNotIn("ERP" + "Next", json.dumps(fake.payloads[0]))
		conv = api.get_conversation(r["conversation"])
		self.assertEqual([m["role"] for m in conv["messages"]], ["user", "assistant"])
		msg = frappe.get_doc("Fibersoft AI Message", state["message"])
		self.assertGreater(msg.cost_usd, 0)
		self.assertIn("count_documents", msg.memo)

	def test_history_and_followup(self):
		r, _ = self._send(["First answer."], message="first")
		r2, fake = self._send(["Second answer."], message="second", conversation=r["conversation"])
		msgs = fake.payloads[0]["messages"]
		self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
		self.assertEqual(msgs[-1]["content"], "second")

	def test_max_steps_forces_answer(self):
		configure(max_tool_iterations=2)
		loop = [[("calculate", {"expressions": {"a": "1+1"}})]] * 5
		r, fake = self._send(loop + ["final"])
		self.assertEqual(fake.payloads[-1].get("tool_choice"), {"type": "none"})
		self.assertEqual(len(fake.payloads), 3)

	def test_action_turn_and_event(self):
		r, fake = self._send([[("propose_create_document", {"doctype": "ToDo", "values": {"description": "from chat"}, "summary": "Create todo"})],
							  "I've prepared the ToDo - please confirm."])
		from fibersoft_ai import api

		state = api.get_turn(r["turn_id"])
		self.assertEqual(len(state["actions"]), 1)
		view = api.execute_action(state["actions"][0]["name"])
		self.assertEqual(view["status"], "Executed")
		roles = frappe.get_all("Fibersoft AI Message", {"conversation": r["conversation"]}, pluck="role", order_by="creation asc")
		self.assertEqual(roles, ["user", "assistant", "event"])
		r2, fake2 = self._send(["ok"], message="thanks", conversation=r["conversation"])
		self.assertIn("System event", json.dumps(fake2.payloads[0]["messages"]))

	def test_limits_and_access(self):
		from fibersoft_ai import api

		used = frappe.db.sql("""select count(*) from `tabFibersoft AI Message` m join `tabFibersoft AI Conversation` c
			on c.name = m.conversation where c.user = %s and m.role = 'user' and m.creation >= CURDATE()""", frappe.session.user)[0][0]
		configure(daily_message_limit=used + 1)
		self._send(["one"])
		with self.assertRaises(frappe.ValidationError):
			self._send(["two"])
		configure(enabled=0)
		with self.assertRaises(frappe.PermissionError):
			api.send_message("hi")

	def test_role_restriction_and_boot(self):
		from fibersoft_ai.boot import widget_config

		configure()
		s = frappe.get_single("Fibersoft AI Settings")
		s.set("allowed_roles", [{"role": "Accounts Manager"}])
		s.save(ignore_permissions=True)
		clear_local_cache()
		make_user(TEST_USER, ["Sales User"])
		frappe.set_user(TEST_USER)
		self.assertFalse(widget_config()["allowed"])
		frappe.set_user("Administrator")
		cfg = widget_config()
		self.assertTrue(cfg["allowed"])
		self.assertNotIn("api_key", json.dumps(cfg))

	def test_conversation_privacy(self):
		r, _ = self._send(["hello"])
		make_user(OTHER_USER, ["System Manager"])  # manager role to pass access, still not owner
		frappe.set_user(OTHER_USER)
		from fibersoft_ai import api

		with self.assertRaises(frappe.PermissionError):
			api.get_conversation(r["conversation"])
		with self.assertRaises(frappe.PermissionError):
			api.get_turn(r["turn_id"])

	def test_stop_button_cancels_running_turn(self):
		from fibersoft_ai.ai import engine as eng

		class Cancelling(FakeClient):
			def create(self, payload, on_text=None, should_cancel=None):
				if len(self.payloads) == 1:
					# user presses Stop while the answer is being produced
					frappe.cache.set_value(eng.cancel_key(self.turn_id), 1, expires_in_sec=60)
					if should_cancel and should_cancel():
						from fibersoft_ai.ai.claude_client import StreamCancelled

						raise StreamCancelled()
				return super().create(payload, on_text, should_cancel)

		fake = Cancelling([[("calculate", {"expressions": {"a": "1+1"}})], "never reached"])
		orig = eng.Turn.__init__

		def init(turn, *a, **k):
			orig(turn, *a, **k)
			fake.turn_id = turn.turn_id
			turn.cancelled()  # warm frappe's local cache, as a long job would

		with patch("fibersoft_ai.ai.engine.ClaudeClient", lambda *a, **k: fake), patch.object(eng.Turn, "__init__", init):
			from fibersoft_ai import api

			r = api.send_message("stop me")
		state = api.get_turn(r["turn_id"])
		self.assertIn("Stopped", state["text"])
		self.assertEqual(frappe.db.get_value("Fibersoft AI Message", state["message"], "status"), "Stopped")

	def test_api_error_is_friendly(self):
		from fibersoft_ai.ai.claude_client import ClaudeAPIError

		class Boom:
			def create(self, *a, **k):
				raise ClaudeAPIError("invalid x-api-key", status=401)

		with patch("fibersoft_ai.ai.engine.ClaudeClient", lambda *a, **k: Boom()):
			from fibersoft_ai import api

			r = api.send_message("hi")
		state = api.get_turn(r["turn_id"])
		self.assertEqual(state["status"], "error")
		self.assertIn("API key is invalid", state["error"])


class TestTokenLimits(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		configure()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Fibersoft AI Token Limit")
		self.user_a = make_user("fai_limit_a@example.com", ["Sales User", "Accounts User"])
		self.user_b = make_user("fai_limit_b@example.com", ["Sales User"])
		frappe.db.delete("Fibersoft AI Message", {"user": ["in", [self.user_a, self.user_b]]})

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Fibersoft AI Token Limit")
		configure()

	def rule(self, **kw):
		return frappe.get_doc(dict({"doctype": "Fibersoft AI Token Limit", "enabled": 1}, **kw)).insert()

	def spend(self, user, tokens, days_ago=0):
		conv = frappe.get_doc({"doctype": "Fibersoft AI Conversation", "title": "usage", "user": user}).insert(ignore_permissions=True)
		m = frappe.get_doc({"doctype": "Fibersoft AI Message", "conversation": conv.name, "role": "assistant", "user": user,
							"billable_tokens": tokens, "cost_usd": tokens / 1e6}).insert(ignore_permissions=True)
		if days_ago:
			frappe.db.set_value("Fibersoft AI Message", m.name, "creation", add_days(nowdate(), -days_ago))
		return conv.name

	def test_rule_resolution_order(self):
		from fibersoft_ai.ai import limits

		self.assertIsNone(limits.effective_rule(self.user_a))
		every = self.rule(limit_for="Everyone", daily_token_limit=1000)
		self.assertEqual(limits.effective_rule(self.user_a).name, every.name)
		low = self.rule(limit_for="Role", role="Sales User", priority=1, daily_token_limit=2000)
		high = self.rule(limit_for="Role", role="Accounts User", priority=5, daily_token_limit=50000)
		self.assertEqual(limits.effective_rule(self.user_a).name, high.name)  # highest priority role
		self.assertEqual(limits.effective_rule(self.user_b).name, low.name)
		mine = self.rule(limit_for="User", user=self.user_a, daily_token_limit=100000, model="claude-haiku-5-5")
		self.assertEqual(limits.effective_rule(self.user_a).name, mine.name)  # user rule wins
		self.assertEqual(limits.status(self.user_a)["model"], "claude-haiku-5-5")
		frappe.db.set_value("Fibersoft AI Token Limit", mine.name, "enabled", 0)
		self.assertEqual(limits.effective_rule(self.user_a).name, high.name)

	def test_validation(self):
		self.rule(limit_for="User", user=self.user_a, daily_token_limit=10)
		with self.assertRaises(frappe.ValidationError):
			self.rule(limit_for="User", user=self.user_a, daily_token_limit=20)  # duplicate
		with self.assertRaises(frappe.ValidationError):
			self.rule(limit_for="User", user=self.user_b, daily_token_limit=5000, weekly_token_limit=1000)
		with self.assertRaises(frappe.ValidationError):
			self.rule(limit_for="Role", role="Sales User", daily_token_limit=-1)
		r = self.rule(limit_for="Everyone", monthly_token_limit=10)
		self.assertEqual(r.title, "Everyone")

	def test_usage_periods_and_blocking(self):
		from fibersoft_ai.ai import limits

		self.rule(limit_for="User", user=self.user_b, daily_token_limit=2000, weekly_token_limit=10000, monthly_token_limit=50000)
		self.spend(self.user_b, 1500)
		self.spend(self.user_b, 40000, days_ago=40)  # outside this month - must not count
		st = limits.status(self.user_b)
		self.assertEqual(st["periods"]["daily"]["used"], 1500)
		self.assertEqual(st["periods"]["monthly"]["used"], 1500)
		self.assertEqual(st["remaining_tokens"], 500)
		limits.assert_allowed(self.user_b)  # still under
		self.spend(self.user_b, 600)
		with self.assertRaises(frappe.ValidationError) as cm:
			limits.assert_allowed(self.user_b)
		self.assertIn("daily allocation", str(cm.exception))
		# user A (no rule) is not affected by user B's limit
		self.spend(self.user_a, 100000)
		limits.assert_allowed(self.user_a)

	def test_billable_formula(self):
		from fibersoft_ai.ai import limits

		self.assertEqual(limits.billable_tokens({"input_tokens": 1000, "output_tokens": 200,
												"cache_creation_input_tokens": 300, "cache_read_input_tokens": 5000}), 2000)

	def test_turn_stops_at_allocation_and_uses_model_override(self):
		frappe.set_user("Administrator")
		self.rule(limit_for="User", user="Administrator", daily_token_limit=1500 + limits_used_today("Administrator"),
				  model="claude-haiku-5-5")
		loop = [[("calculate", {"expressions": {"a": "1+1"}})]] * 6
		fake = FakeClient(loop + ["final answer"])
		with patch("fibersoft_ai.ai.engine.ClaudeClient", lambda *a, **k: fake):
			from fibersoft_ai import api

			r = api.send_message("loop please")
		# each fake call = 1000 in + 100 out + 10% of 500 cached = 1150 counted tokens
		self.assertEqual(fake.payloads[0]["model"], "claude-haiku-5-5")
		self.assertEqual(len(fake.payloads), 3)
		self.assertEqual(fake.payloads[2]["tool_choice"], {"type": "none"})
		state = api.get_turn(r["turn_id"])
		self.assertIn("token allocation was reached", state["text"])
		msg = frappe.get_doc("Fibersoft AI Message", state["message"])
		self.assertEqual(msg.billable_tokens, 3450)
		self.assertEqual(msg.user, "Administrator")
		with self.assertRaises(frappe.ValidationError):
			api.send_message("one more")  # now over the daily allocation

	def test_deleting_chat_keeps_usage(self):
		from fibersoft_ai import api
		from fibersoft_ai.ai import limits

		frappe.set_user(self.user_b)
		configure()
		frappe.set_user(self.user_b)
		conv = self.spend(self.user_b, 700)
		before = limits.status(self.user_b)["periods"]["daily"]["used"]
		api.delete_conversation(conv)
		self.assertEqual(limits.status(self.user_b)["periods"]["daily"]["used"], before)
		self.assertNotIn(conv, [c.name for c in api.list_conversations()])
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_conversation(conv)

	def test_busy_lock(self):
		from fibersoft_ai import api
		from fibersoft_ai.ai import engine as eng

		import time as _t

		frappe.cache.set_value(eng.running_key("Administrator"), "busyturn", expires_in_sec=60)
		eng.set_state("busyturn", {"user": "Administrator", "status": "running"})
		try:
			with self.assertRaises(frappe.ValidationError):
				api.send_message("hello")
			eng.set_state("busyturn", {"user": "Administrator", "status": "queued", "queued_at": _t.time() - 600})
			r, _ = TestEngineAndApi._send(self, ["ok"])  # stale queued lock is ignored
			self.assertTrue(r["turn_id"])
		finally:
			frappe.cache.delete_value(eng.running_key("Administrator"))

	def test_usage_report_and_copy(self):
		from frappe.desk.query_report import run

		from fibersoft_ai import api

		src = self.rule(limit_for="Role", role="Sales User", daily_token_limit=3000, monthly_token_limit=60000)
		self.spend(self.user_b, 1200)
		res = run("Fibersoft AI Usage", filters={"period": "Today"}, ignore_prepared_report=True)
		row = next(r for r in res["result"] if r["user"] == self.user_b)
		self.assertEqual(row["tokens"], 1200)
		self.assertEqual(row["token_limit"], 3000)
		self.assertEqual(row["used_pct"], 40.0)
		self.assertTrue(res["report_summary"])
		out = api.copy_limit_to_users(src.name, json.dumps([self.user_a, self.user_b, "nobody@x.com"]))
		self.assertEqual(out, {"created": 2, "skipped": 1})
		self.assertEqual(frappe.db.get_value("Fibersoft AI Token Limit", {"user": self.user_a}, "daily_token_limit"), 3000)
		data = api.get_user_usage(rule=src.name)
		governed = {u["user"] for u in data["users"]}
		self.assertFalse(governed & {self.user_a, self.user_b})  # both now have their own User rules


def limits_used_today(user):
	from fibersoft_ai.ai import limits

	return limits.status(user)["periods"]["daily"]["used"]
