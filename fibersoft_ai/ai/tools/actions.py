"""Action tools. The model only PROPOSES; the user confirms in the chat before anything changes.

Safety layers for every action:
  1. Allow Actions + an Action Rule for the DocType + the specific action type ticked.
  2. Never for system DocTypes, child tables or single DocTypes.
  3. The user's own Frappe permissions (create / write / submit / cancel / workflow role), checked at
     proposal time AND again at execution time.
  4. Field-level validation (unknown / read-only / permlevel / link existence / select options / types).
  5. Optional dry run of the real operation in a rolled-back savepoint.
  6. Execution only by the same user, only while Pending and not expired, and only if the target
     document has not been modified since the proposal.
"""

import copy
import json

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime, add_to_date

from fibersoft_ai.ai.dryrun import DryRunCommitAttempt, dry_run
from fibersoft_ai.ai.guard import (
	ALWAYS_READ_ONLY_DOCTYPES,
	NO_VALUE_TYPES,
	TABLE_TYPES,
	ToolError,
	assert_doctype_allowed,
	compact_value,
	is_sensitive,
	redacted_fields,
)
from fibersoft_ai.ai.settings import action_rule, get_settings
from fibersoft_ai.ai.tools import tool
from fibersoft_ai.ai.tools.query import doc_url

PROTECTED_KEYS = {
	"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "doctype",
	"amended_from", "parent", "parenttype", "parentfield", "_user_tags", "_comments", "_assign",
	"_liked_by", "_seen", "naming_series_counter",
}
SUMMARY_SCHEMA = {
	"type": "string",
	"description": "One short sentence describing the change for the user, e.g. 'Create Sales Order for Ali Traders: 50 x Shirt @ 1,200'",
}


# ----------------------------------------------------------------------------- checks
def check_rule(doctype, kind):
	assert_doctype_allowed(doctype)
	meta = frappe.get_meta(doctype)
	if doctype in ALWAYS_READ_ONLY_DOCTYPES or meta.istable or meta.issingle:
		raise ToolError(_("{0} cannot be changed by the assistant.").format(doctype))
	rule = action_rule(doctype)
	if not rule or not cint(rule.get(f"allow_{kind}")):
		raise ToolError(
			_("'{0}' actions on {1} are not enabled. A System Manager can enable them in Fibersoft AI Settings > Action Rules. "
			  "Tell the user you cannot do this here, and how they can do it themselves.").format(kind, doctype)
		)
	return meta


def check_perm(doctype, ptype, doc=None, user=None):
	if not frappe.has_permission(doctype, ptype, doc=doc, user=user):
		raise ToolError(
			_("Permission denied: user {0} does not have '{1}' permission on {2}{3}.").format(
				user or frappe.session.user, ptype, doctype, f" {doc.name}" if doc else ""
			)
		)


def get_existing(doctype, name):
	if not name or not frappe.db.exists(doctype, name):
		raise ToolError(_("{0} '{1}' not found. Use find_records.").format(doctype, name))
	return frappe.get_doc(doctype, name)


def writable_levels(meta_for_perm):
	return set(meta_for_perm.get_permlevel_access("write") or []) | {0}


def validate_values(meta, values, mode, doc=None, perm_meta=None, path=""):
	"""Validate and coerce values. mode: 'create' | 'update'. Returns cleaned dict."""
	if not isinstance(values, dict) or not values:
		raise ToolError(_("{0}values must be a non-empty object of fieldname -> value").format(path))
	perm_meta = perm_meta or meta
	levels = writable_levels(perm_meta)
	redacted = redacted_fields(meta.name)
	submitted = bool(doc and cint(doc.docstatus) == 1)
	cleaned, errors = {}, []

	for key, value in values.items():
		where = f"{path}{key}"
		if key in PROTECTED_KEYS and not (meta.istable and key == "name" and mode == "update"):
			if key == "name" and mode == "create" and meta.autoname in ("prompt", "Prompt"):
				cleaned["__newname"] = value
				continue
			errors.append(_("{0}: is a system field and cannot be set").format(where))
			continue
		if meta.istable and key == "name":
			cleaned["name"] = value
			continue
		df = meta.get_field(key)
		if not df:
			errors.append(_("{0}: {1} has no field '{2}'").format(where, meta.name, key))
			continue
		if df.fieldtype in NO_VALUE_TYPES or is_sensitive(df) or key in redacted:
			errors.append(_("{0}: cannot be set by the assistant").format(where))
			continue
		if cint(df.permlevel) not in levels:
			errors.append(_("{0}: you do not have permission to edit this field").format(where))
			continue
		if df.read_only and not df.fieldtype in TABLE_TYPES:
			errors.append(_("{0}: is read-only / calculated automatically - do not set it").format(where))
			continue
		if submitted and not df.allow_on_submit:
			errors.append(_("{0}: cannot be changed after submit (document is submitted)").format(where))
			continue

		try:
			cleaned[key] = coerce(df, value, values, doc, mode, where, perm_meta)
		except ToolError as e:
			errors.append(str(e))

	if errors:
		if path:  # child row - the parent adds the prefix
			raise ToolError("; ".join(errors[:15]))
		raise ToolError(_("Please fix: ") + "; ".join(errors[:15]))
	return cleaned


def coerce(df, value, values, doc, mode, where, perm_meta):
	ft = df.fieldtype
	if value is None or value == "":
		if df.reqd and mode == "update":
			raise ToolError(_("{0}: is mandatory and cannot be cleared").format(where))
		return None if ft not in ("Data", "Small Text", "Text", "Long Text", "Text Editor", "Select") else ""
	if ft in TABLE_TYPES:
		if not isinstance(value, list):
			raise ToolError(_("{0}: must be a list of row objects").format(where))
		child_meta = frappe.get_meta(df.options)
		rows = []
		existing = {r.name for r in (doc.get(df.fieldname) or [])} if doc else set()
		for i, row in enumerate(value):
			rp = f"{where}[{i}]."
			if ft == "Table MultiSelect" and isinstance(row, str):
				link_field = next((f for f in child_meta.fields if f.fieldtype == "Link"), None)
				row = {link_field.fieldname: row} if link_field else {}
			if not isinstance(row, dict):
				raise ToolError(_("{0}: each row must be an object").format(where))
			rmode = "update" if row.get("name") else "create"
			if row.get("name") and row["name"] not in existing:
				raise ToolError(_("{0}name: row '{1}' does not exist in this document").format(rp, row["name"]))
			if rmode == "create" and not {k for k in row if k != "name"}:
				raise ToolError(_("{0}: row is empty").format(rp))
			rows.append(validate_values(child_meta, row, rmode, doc=doc, perm_meta=perm_meta, path=rp))
		return rows
	if ft == "Link":
		value = str(value).strip()
		if not frappe.db.exists(df.options, value):
			raise ToolError(_("{0}: {1} '{2}' does not exist (use find_records to get the exact name)").format(where, df.options, value))
		return value
	if ft == "Dynamic Link":
		target = values.get(df.options) or (doc.get(df.options) if doc else None)
		if not target:
			raise ToolError(_("{0}: set '{1}' (the DocType) as well").format(where, df.options))
		if not frappe.db.exists(target, value):
			raise ToolError(_("{0}: {1} '{2}' does not exist").format(where, target, value))
		return value
	if ft == "Select":
		options = [o for o in (df.options or "").split("\n")]
		if options and str(value) not in options:
			raise ToolError(_("{0}: '{1}' is not a valid option. Options: {2}").format(where, value, ", ".join(o for o in options if o)))
		return str(value)
	if ft in ("Int", "Check"):
		try:
			v = int(float(value)) if not isinstance(value, bool) else int(value)
		except (TypeError, ValueError):
			raise ToolError(_("{0}: must be a whole number").format(where))
		if ft == "Check" and v not in (0, 1):
			raise ToolError(_("{0}: must be 0 or 1").format(where))
		return v
	if ft in ("Float", "Currency", "Percent"):
		try:
			return float(str(value).replace(",", ""))
		except (TypeError, ValueError):
			raise ToolError(_("{0}: must be a number").format(where))
	if ft == "Date":
		try:
			return str(getdate(value))
		except Exception:
			raise ToolError(_("{0}: invalid date '{1}' (use YYYY-MM-DD)").format(where, value))
	if ft == "Datetime":
		try:
			return str(get_datetime(value))
		except Exception:
			raise ToolError(_("{0}: invalid datetime '{1}'").format(where, value))
	if isinstance(value, (dict, list)):
		raise ToolError(_("{0}: must be text").format(where))
	value = str(value)
	if ft == "Data" and df.length and len(value) > cint(df.length):
		raise ToolError(_("{0}: longer than {1} characters").format(where, df.length))
	return value


def apply_values(doc, values, remove_rows=None):
	"""Apply validated values to a document (child rows: with name = update, without = append)."""
	for key, value in values.items():
		if key == "__newname":
			continue
		df = doc.meta.get_field(key)
		if df and df.fieldtype in TABLE_TYPES:
			for row in value:
				if row.get("name"):
					target = next(r for r in doc.get(key) if r.name == row["name"])
					target.update({k: v for k, v in row.items() if k != "name"})
				else:
					doc.append(key, row)
		else:
			doc.set(key, value)
	for table, names in (remove_rows or {}).items():
		names = set(names or [])
		doc.set(table, [r for r in doc.get(table) if r.name not in names])


def snapshot(doc, keys, remove_rows=None):
	snap = {}
	for key in set(keys) | set((remove_rows or {}).keys()):
		if key == "__newname":
			continue
		df = doc.meta.get_field(key)
		if df and df.fieldtype in TABLE_TYPES:
			snap[key] = [
				{k: v for k, v in r.as_dict(no_default_fields=True).items() if not k.startswith("__")} | {"name": r.name}
				for r in doc.get(key)
			]
		else:
			snap[key] = doc.get(key)
	return json.loads(json.dumps(snap, default=str))


def try_dry_run(fn):
	"""Returns list of notes. Raises ToolError with the document's own validation message on failure."""
	if not cint(get_settings().validate_before_proposing):
		return ["Not dry-run validated (disabled in settings); final validation happens on Confirm."]
	try:
		with dry_run():
			result = fn()
		return result if isinstance(result, list) else []
	except DryRunCommitAttempt:
		return ["This action could not be test-run safely; it will be fully validated when you confirm."]
	except ToolError:
		raise
	except frappe.PermissionError as e:
		raise ToolError(_("Permission denied in dry run: {0}").format(_msg(e)))
	except Exception as e:
		raise ToolError(_("Dry run failed - the system rejected this change: {0}. Fix the values (or tell the user) and try again.").format(_msg(e)))


def _msg(e):
	from fibersoft_ai.ai.tools import _clean_message

	return _clean_message(e) or type(e).__name__


def create_action(ctx, action_type, doctype, name, payload, summary, notes=None, undoable=0, preview=None):
	settings = get_settings()
	action = frappe.get_doc({
		"doctype": "Fibersoft AI Action",
		"action_type": action_type,
		"status": "Pending",
		"user": frappe.session.user,
		"conversation": ctx.get("conversation"),
		"turn_id": ctx.get("turn_id"),
		"reference_doctype": doctype,
		"reference_name": name,
		"summary": (summary or f"{action_type} {doctype} {name or ''}").strip()[:500],
		"payload": json.dumps(dict(payload, preview=preview or {}), default=str, indent=1),
		"warnings": "\n".join(notes or []) or None,
		"undoable": undoable,
	})
	action.flags.ignore_permissions = True
	action.insert()
	view = action_view(action)
	ctx.setdefault("actions", []).append(view)
	return {
		"action_id": action.name,
		"status": "awaiting_user_confirmation",
		"expires_in_hours": cint(settings.action_expiry_hours) or 24,
		"preview": preview,
		"notes": notes or [],
		"instruction": "The user now sees this action with Confirm / Reject buttons. It has NOT been executed. "
		"Briefly tell the user what will happen and ask them to review and confirm.",
	}


def action_view(action):
	payload = json.loads(action.payload or "{}")
	return {
		"name": action.name,
		"action_type": action.action_type,
		"status": action.status,
		"reference_doctype": action.reference_doctype,
		"reference_name": action.reference_name,
		"summary": action.summary,
		"preview": payload.get("preview") or {},
		"warnings": action.warnings,
		"result": action.result,
		"undoable": cint(action.undoable),
		"url": doc_url(action.reference_doctype, action.reference_name) if action.reference_name else None,
		"creation": str(action.creation),
	}


def _preview_values(meta, values):
	out = {}
	for k, v in values.items():
		if k == "__newname":
			out["name"] = v
			continue
		label = meta.get_label(k) or k
		df = meta.get_field(k)
		if df and df.fieldtype in TABLE_TYPES:
			child = frappe.get_meta(df.options)
			out[label] = [{child.get_label(ck) or ck: compact_value(cv) for ck, cv in r.items()} for r in v][:50]
		else:
			out[label] = compact_value(v)
	return out


KEY_TOTALS = ("total_qty", "net_total", "total_taxes_and_charges", "grand_total", "rounded_total",
			  "outstanding_amount", "total_debit", "total_credit", "paid_amount", "total", "amount")


def _computed(doc):
	out = {}
	for k in KEY_TOTALS:
		if doc.meta.get_field(k) and doc.get(k) not in (None, ""):
			out[doc.meta.get_label(k)] = compact_value(doc.get(k))
	return out


# ----------------------------------------------------------------------------- proposals
@tool(
	"propose_create_document",
	"""Prepare a NEW document for the user to confirm. Validates fields and test-runs the insert; if it fails
you get the system's error message — fix the values and try again. Child-table rows go in a list under the
table fieldname, e.g. {"customer":"Ali Traders","items":[{"item_code":"SHIRT-01","qty":50,"rate":1200}]}.
Resolve every Link value with find_records first. Never claim the document was created until the user confirms.""",
	{
		"doctype": {"type": "string"},
		"values": {"type": "object", "description": "fieldname -> value, including child-table lists"},
		"summary": SUMMARY_SCHEMA,
	},
	["doctype", "values", "summary"],
	kind="action",
)
def propose_create_document(ctx, doctype, values, summary=None):
	meta = check_rule(doctype, "create")
	check_perm(doctype, "create")
	cleaned = validate_values(meta, values, "create")
	computed = {}

	def run():
		doc = frappe.get_doc(dict({"doctype": doctype}, **copy.deepcopy({k: v for k, v in cleaned.items() if k != "__newname"})))
		if cleaned.get("__newname"):
			doc.flags.name_set = True
			doc.name = cleaned["__newname"]
		doc.insert()
		computed.update(_computed(doc))
		return []

	notes = try_dry_run(run)
	preview = {"values": _preview_values(meta, cleaned)}
	if computed:
		preview["calculated"] = computed
	return create_action(ctx, "Create", doctype, None, {"values": cleaned}, summary, notes, undoable=1, preview=preview)


@tool(
	"propose_update_document",
	"""Prepare changes to an existing document for the user to confirm. Only the given fields change.
For child tables: rows WITH "name" (the row id from get_document) update that row; rows WITHOUT "name" are
appended. Remove rows with remove_rows {"items": ["row-id", ...]}. Submitted documents only allow
'allow_on_submit' fields.""",
	{
		"doctype": {"type": "string"},
		"name": {"type": "string"},
		"values": {"type": "object"},
		"remove_rows": {"type": "object", "description": "{table_fieldname: [row names]}"},
		"summary": SUMMARY_SCHEMA,
	},
	["doctype", "name", "values", "summary"],
	kind="action",
)
def propose_update_document(ctx, doctype, name, values, remove_rows=None, summary=None):
	meta = check_rule(doctype, "update")
	doc = get_existing(doctype, name)
	check_perm(doctype, "write", doc)
	if cint(doc.docstatus) == 2:
		raise ToolError(_("{0} {1} is cancelled and cannot be changed (amend it instead).").format(doctype, name))
	cleaned = validate_values(meta, values, "update", doc=doc) if values else {}
	remove_rows = remove_rows or {}
	for table, names in remove_rows.items():
		df = meta.get_field(table)
		if not df or df.fieldtype not in TABLE_TYPES:
			raise ToolError(_("remove_rows: '{0}' is not a child table").format(table))
		if cint(doc.docstatus) == 1 and not df.allow_on_submit:
			raise ToolError(_("remove_rows: cannot change {0} after submit").format(table))
		existing = {r.name for r in doc.get(table)}
		missing = set(names or []) - existing
		if missing:
			raise ToolError(_("remove_rows: rows {0} not found in {1}").format(", ".join(missing), table))
	if not cleaned and not remove_rows:
		raise ToolError(_("Nothing to change"))

	before = snapshot(doc, cleaned.keys(), remove_rows)
	changes = {}
	for k, v in cleaned.items():
		df = meta.get_field(k)
		if df.fieldtype in TABLE_TYPES:
			changes[meta.get_label(k)] = {"rows_changed_or_added": _preview_values(meta, {k: v})[meta.get_label(k)]}
		elif str(before.get(k) or "") != str(v or ""):
			changes[meta.get_label(k)] = {"from": compact_value(before.get(k)), "to": compact_value(v)}
	for table, names in remove_rows.items():
		changes.setdefault(meta.get_label(table), {})["rows_removed"] = list(names)
	if not changes:
		raise ToolError(_("These values are already set - nothing would change."))
	computed = {}

	def run():
		d = frappe.get_doc(doctype, name)
		apply_values(d, copy.deepcopy(cleaned), remove_rows)
		d.save()
		computed.update(_computed(d))

	notes = try_dry_run(run)
	preview = {"document": name, "changes": changes}
	if computed:
		preview["calculated_after_change"] = computed
	return create_action(
		ctx, "Update", doctype, name,
		{"values": cleaned, "remove_rows": remove_rows, "modified": str(doc.modified)},
		summary, notes, undoable=1, preview=preview,
	)


def _no_workflow(doctype):
	wf = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
	if wf:
		raise ToolError(_("{0} is controlled by workflow '{1}'. Use propose_workflow_action with one of the available actions.").format(doctype, wf))


@tool(
	"propose_submit_document",
	"Prepare submitting a draft document (docstatus 0 → 1) for the user to confirm. Test-runs the submit first.",
	{"doctype": {"type": "string"}, "name": {"type": "string"}, "summary": SUMMARY_SCHEMA},
	["doctype", "name", "summary"],
	kind="action",
)
def propose_submit_document(ctx, doctype, name, summary=None):
	meta = check_rule(doctype, "submit")
	if not meta.is_submittable:
		raise ToolError(_("{0} is not submittable").format(doctype))
	doc = get_existing(doctype, name)
	if cint(doc.docstatus) != 0:
		raise ToolError(_("{0} {1} is not a draft").format(doctype, name))
	check_perm(doctype, "submit", doc)
	_no_workflow(doctype)
	notes = try_dry_run(lambda: frappe.get_doc(doctype, name).submit())
	return create_action(ctx, "Submit", doctype, name, {"modified": str(doc.modified)}, summary, notes,
						 preview={"document": name, "change": "Draft → Submitted", **_computed(doc)})


@tool(
	"propose_cancel_document",
	"Prepare cancelling a submitted document (docstatus 1 → 2) for the user to confirm. Test-runs the cancel first (linked documents may block it).",
	{"doctype": {"type": "string"}, "name": {"type": "string"}, "summary": SUMMARY_SCHEMA},
	["doctype", "name", "summary"],
	kind="action",
)
def propose_cancel_document(ctx, doctype, name, summary=None):
	meta = check_rule(doctype, "cancel")
	doc = get_existing(doctype, name)
	if not meta.is_submittable or cint(doc.docstatus) != 1:
		raise ToolError(_("{0} {1} is not a submitted document").format(doctype, name))
	check_perm(doctype, "cancel", doc)
	_no_workflow(doctype)
	notes = try_dry_run(lambda: frappe.get_doc(doctype, name).cancel())
	return create_action(ctx, "Cancel", doctype, name, {"modified": str(doc.modified)}, summary, notes,
						 preview={"document": name, "change": "Submitted → Cancelled", **_computed(doc)})


@tool(
	"propose_workflow_action",
	"""Prepare a workflow action (e.g. Approve, Reject, Send for Approval) on a document for the user to confirm.
Only actions the user is allowed to take from the current state are accepted; the error lists them.""",
	{
		"doctype": {"type": "string"},
		"name": {"type": "string"},
		"action": {"type": "string", "description": "Workflow action name exactly, e.g. 'Approve'"},
		"summary": SUMMARY_SCHEMA,
	},
	["doctype", "name", "action", "summary"],
	kind="action",
)
def propose_workflow_action(ctx, doctype, name, action, summary=None):
	check_rule(doctype, "workflow")
	doc = get_existing(doctype, name)
	check_perm(doctype, "read", doc)
	from frappe.model.workflow import get_transitions, get_workflow_name

	wf_name = get_workflow_name(doctype)
	if not wf_name:
		raise ToolError(_("{0} has no active workflow").format(doctype))
	transitions = get_transitions(doc)
	available = [t.get("action") for t in transitions]
	match = next((t for t in transitions if str(t.get("action")).lower() == str(action).lower()), None)
	if not match:
		raise ToolError(_("'{0}' is not available to you for {1} {2} in its current state. Available: {3}").format(
			action, doctype, name, ", ".join(available) or "none"))
	state_field = frappe.get_cached_value("Workflow", wf_name, "workflow_state_field")
	from frappe.model.workflow import apply_workflow

	notes = try_dry_run(lambda: apply_workflow(frappe.get_doc(doctype, name), match.get("action")))
	return create_action(
		ctx, "Workflow", doctype, name, {"action": match.get("action"), "modified": str(doc.modified)}, summary, notes,
		preview={"document": name, "action": match.get("action"), "from_state": doc.get(state_field), "to_state": match.get("next_state")},
	)


@tool(
	"propose_comment",
	"Prepare adding a comment to a document's timeline, for the user to confirm.",
	{"doctype": {"type": "string"}, "name": {"type": "string"}, "comment": {"type": "string"}, "summary": SUMMARY_SCHEMA},
	["doctype", "name", "comment"],
	kind="action",
)
def propose_comment(ctx, doctype, name, comment, summary=None):
	check_rule(doctype, "comment")
	doc = get_existing(doctype, name)
	check_perm(doctype, "read", doc)
	comment = (comment or "").strip()
	if not comment:
		raise ToolError(_("Comment text is empty"))
	if len(comment) > 5000:
		raise ToolError(_("Comment is too long"))
	return create_action(ctx, "Comment", doctype, name, {"comment": comment}, summary or _("Add comment to {0} {1}").format(doctype, name),
						 undoable=1, preview={"document": name, "comment": comment})


@tool(
	"propose_assignment",
	"Prepare assigning a document to one or more users (creates a ToDo for them), for the user to confirm.",
	{
		"doctype": {"type": "string"},
		"name": {"type": "string"},
		"assign_to": {"type": "array", "items": {"type": "string"}, "description": "User IDs (emails). Use find_records on User."},
		"description": {"type": "string"},
		"date": {"type": "string", "description": "Due date YYYY-MM-DD"},
		"priority": {"type": "string", "enum": ["Low", "Medium", "High"]},
		"summary": SUMMARY_SCHEMA,
	},
	["doctype", "name", "assign_to"],
	kind="action",
)
def propose_assignment(ctx, doctype, name, assign_to, description=None, date=None, priority="Medium", summary=None):
	check_rule(doctype, "assign")
	doc = get_existing(doctype, name)
	check_perm(doctype, "read", doc)
	if isinstance(assign_to, str):
		assign_to = [assign_to]
	users = []
	for u in assign_to or []:
		row = frappe.db.get_value("User", u, ["name", "enabled", "full_name"], as_dict=True) or frappe.db.get_value(
			"User", {"full_name": u}, ["name", "enabled", "full_name"], as_dict=True)
		if not row:
			raise ToolError(_("User '{0}' not found. Use find_records on User.").format(u))
		if not row.enabled:
			raise ToolError(_("User {0} is disabled").format(row.name))
		if not frappe.has_permission(doctype, "read", doc=doc, user=row.name):
			raise ToolError(_("{0} cannot read {1} {2}, so it cannot be assigned to them").format(row.name, doctype, name))
		users.append(row.name)
	if not users:
		raise ToolError(_("assign_to is empty"))
	if date:
		date = str(getdate(date))
	payload = {"assign_to": users, "description": description or "", "date": date, "priority": priority or "Medium"}
	return create_action(ctx, "Assign", doctype, name, payload, summary or _("Assign {0} {1} to {2}").format(doctype, name, ", ".join(users)),
						 undoable=1, preview={"document": name, "assign_to": users, "due": date, "note": description})


# ----------------------------------------------------------------------------- execution (called from API)
def load_action_for_user(action_name):
	if not frappe.db.exists("Fibersoft AI Action", action_name):
		frappe.throw(_("Action not found"), frappe.DoesNotExistError)
	action = frappe.get_doc("Fibersoft AI Action", action_name)
	if action.user != frappe.session.user:
		frappe.throw(_("This action belongs to another user"), frappe.PermissionError)
	return action


def _assert_unchanged(doc, payload):
	if payload.get("modified") and str(doc.modified) != payload["modified"]:
		frappe.throw(_("{0} {1} was modified after this action was prepared. Please ask the assistant again so it uses the latest version.").format(doc.doctype, doc.name))


def execute(action_name):
	action = load_action_for_user(action_name)
	if action.status != "Pending":
		frappe.throw(_("This action is already {0}").format(action.status))
	hours = cint(get_settings().action_expiry_hours) or 24
	if get_datetime(action.creation) < add_to_date(now_datetime(), hours=-hours):
		action.db_set("status", "Expired")
		frappe.throw(_("This action has expired. Please ask again."))

	payload = json.loads(action.payload or "{}")
	dt, name = action.reference_doctype, action.reference_name
	kind = {"Create": "create", "Update": "update", "Submit": "submit", "Cancel": "cancel",
			"Workflow": "workflow", "Comment": "comment", "Assign": "assign"}[action.action_type]
	try:
		meta = check_rule(dt, kind)
	except ToolError as e:
		frappe.throw(str(e))

	result, previous, ref_name = "", None, name
	try:
		if action.action_type == "Create":
			check_perm(dt, "create")
			values = validate_values(meta, payload["values"], "create")
			doc = frappe.get_doc(dict({"doctype": dt}, **copy.deepcopy({k: v for k, v in values.items() if k != "__newname"})))
			if values.get("__newname"):
				doc.flags.name_set = True
				doc.name = values["__newname"]
			doc.insert()
			ref_name, result = doc.name, _("Created {0} {1}").format(dt, doc.name)
			previous = {"created": doc.name, "modified": str(doc.modified)}
		elif action.action_type == "Update":
			doc = frappe.get_doc(dt, name)
			check_perm(dt, "write", doc)
			_assert_unchanged(doc, payload)
			values = validate_values(meta, payload.get("values") or {}, "update", doc=doc) if payload.get("values") else {}
			previous = {"values": snapshot(doc, values.keys(), payload.get("remove_rows"))}
			apply_values(doc, copy.deepcopy(values), payload.get("remove_rows"))
			doc.save()
			previous["modified_after"] = str(doc.modified)
			result = _("Updated {0} {1}").format(dt, name)
		elif action.action_type == "Submit":
			doc = frappe.get_doc(dt, name)
			check_perm(dt, "submit", doc)
			_assert_unchanged(doc, payload)
			doc.submit()
			result = _("Submitted {0} {1}").format(dt, name)
		elif action.action_type == "Cancel":
			doc = frappe.get_doc(dt, name)
			check_perm(dt, "cancel", doc)
			_assert_unchanged(doc, payload)
			doc.cancel()
			result = _("Cancelled {0} {1}").format(dt, name)
		elif action.action_type == "Workflow":
			from frappe.model.workflow import apply_workflow

			doc = frappe.get_doc(dt, name)
			_assert_unchanged(doc, payload)
			doc = apply_workflow(doc, payload["action"])
			state_field = frappe.get_cached_value("Workflow", {"document_type": dt, "is_active": 1}, "workflow_state_field")
			result = _("{0} applied - {1} {2} is now '{3}'").format(payload["action"], dt, name, doc.get(state_field) if state_field else "")
		elif action.action_type == "Comment":
			doc = frappe.get_doc(dt, name)
			check_perm(dt, "read", doc)
			c = doc.add_comment("Comment", frappe.utils.escape_html(payload["comment"]).replace("\n", "<br>"))
			previous = {"comment": c.name}
			result = _("Comment added to {0} {1}").format(dt, name)
		elif action.action_type == "Assign":
			from frappe.desk.form import assign_to

			doc = frappe.get_doc(dt, name)
			check_perm(dt, "read", doc)
			assign_to.add({
				"assign_to": payload["assign_to"], "doctype": dt, "name": name,
				"description": payload.get("description") or "", "date": payload.get("date"),
				"priority": payload.get("priority") or "Medium",
			})
			previous = {"assigned": payload["assign_to"]}
			result = _("Assigned {0} {1} to {2}").format(dt, name, ", ".join(payload["assign_to"]))
	except Exception as e:
		frappe.db.rollback()
		action.reload()
		msg = _msg(e)
		action.db_set({"status": "Failed", "result": msg[:1000], "executed_on": now_datetime()})
		frappe.db.commit()
		frappe.clear_messages()
		return dict(action_view(action), error=msg)

	action.db_set({
		"status": "Executed",
		"result": result,
		"reference_name": ref_name,
		"executed_on": now_datetime(),
		"previous_values": json.dumps(previous, default=str) if previous else None,
	})
	frappe.clear_messages()
	return action_view(action)


def reject(action_name):
	action = load_action_for_user(action_name)
	if action.status != "Pending":
		frappe.throw(_("This action is already {0}").format(action.status))
	action.db_set("status", "Rejected")
	return action_view(action)


def undo(action_name):
	action = load_action_for_user(action_name)
	if action.status != "Executed" or not cint(action.undoable):
		frappe.throw(_("This action cannot be undone"))
	prev = json.loads(action.previous_values or "{}")
	dt, name = action.reference_doctype, action.reference_name
	if action.action_type == "Create":
		if not frappe.db.exists(dt, name):
			frappe.throw(_("{0} {1} no longer exists").format(dt, name))
		doc = frappe.get_doc(dt, name)
		if cint(doc.docstatus) != 0:
			frappe.throw(_("{0} {1} has been submitted; cancel it instead of undoing").format(dt, name))
		frappe.delete_doc(dt, name)  # permission-checked
		result = _("Deleted {0} {1}").format(dt, name)
	elif action.action_type == "Update":
		doc = frappe.get_doc(dt, name)
		check_perm(dt, "write", doc)
		if prev.get("modified_after") and str(doc.modified) != prev["modified_after"]:
			frappe.throw(_("{0} {1} has changed since this update, so it cannot be undone automatically").format(dt, name))
		for key, value in (prev.get("values") or {}).items():
			df = doc.meta.get_field(key)
			if df and df.fieldtype in TABLE_TYPES:
				doc.set(key, [])
				for row in value:
					row = {k: v for k, v in row.items() if k not in ("parent", "parenttype", "parentfield", "doctype")}
					doc.append(key, row)
			else:
				doc.set(key, value)
		doc.save()
		result = _("Restored previous values of {0} {1}").format(dt, name)
	elif action.action_type == "Comment":
		if prev.get("comment") and frappe.db.exists("Comment", prev["comment"]):
			frappe.delete_doc("Comment", prev["comment"], ignore_permissions=True)
		result = _("Comment removed")
	elif action.action_type == "Assign":
		from frappe.desk.form import assign_to

		for user in prev.get("assigned") or []:
			assign_to.remove(dt, name, user)
		result = _("Assignment removed")
	else:
		frappe.throw(_("This action cannot be undone"))
	action.db_set({"status": "Undone", "result": result})
	frappe.clear_messages()
	return action_view(action)
