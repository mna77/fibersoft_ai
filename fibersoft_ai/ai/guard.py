"""Access control, redaction and serialisation shared by every assistant tool.

Everything the model reads goes through here. The rules, in order:
1. Frappe's own permissions for the logged-in user (role, user permissions, permlevel).
2. Hard-blocked DocTypes (the assistant's own data and credential stores).
3. Admin-blocked DocTypes and redacted fields from Fibersoft AI Settings.
4. Password-type and credential-like fields are always stripped.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from fibersoft_ai.ai.settings import get_settings


class ToolError(Exception):
	"""An error the model should see and can recover from (bad input, not found, no permission)."""


HARD_BLOCKED_DOCTYPES = {
	"Fibersoft AI Settings",
	"Fibersoft AI Conversation",
	"Fibersoft AI Message",
	"Fibersoft AI Action",
	"OAuth Bearer Token",
	"OAuth Authorization Code",
	"OAuth Client",
	"Token Cache",
	"Social Login Key",
	"User Social Login",
	"Connected App",
	"Access Log",
	"Deleted Document",
}

# never changeable by the assistant, even if an admin adds an Action Rule
ALWAYS_READ_ONLY_DOCTYPES = HARD_BLOCKED_DOCTYPES | {
	"User",
	"Role",
	"Role Profile",
	"Has Role",
	"User Permission",
	"DocShare",
	"DocType",
	"DocField",
	"DocPerm",
	"Custom DocPerm",
	"Custom Field",
	"Property Setter",
	"Customize Form",
	"Server Script",
	"Client Script",
	"System Settings",
	"Report",
	"Workspace",
	"Module Def",
	"Workflow",
	"Workflow State",
	"Workflow Action Master",
	"Print Format",
	"Web Page",
	"Website Settings",
	"Email Account",
	"Scheduled Job Type",
	"Installed Applications",
	"File",
	"Data Import",
	"Version",
	"Comment",
	"Communication",
	"Error Log",
}

SENSITIVE_FIELDNAMES = {
	"api_key",
	"api_secret",
	"password",
	"new_password",
	"secret",
	"client_secret",
	"access_token",
	"refresh_token",
	"private_key",
	"otp_secret",
	"encryption_key",
	"reset_password_key",
	"last_password_reset_date",
}

NO_VALUE_TYPES = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Image", "Fold", "Heading"}
TABLE_TYPES = {"Table", "Table MultiSelect"}
NUMERIC_TYPES = {"Int", "Float", "Currency", "Percent", "Check", "Rating", "Duration"}
STANDARD_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}
CHILD_STANDARD_FIELDS = {"parent", "parenttype", "parentfield"}
NOISE_KEYS = {"_user_tags", "_comments", "_liked_by", "_seen", "__onload", "__last_sync_on", "doctype"}

MAX_TEXT = 3000
MAX_CHILD_ROWS = 150


def blocked_doctypes():
	s = get_settings()
	return HARD_BLOCKED_DOCTYPES | {row.document_type for row in (s.blocked_doctypes or [])}


def redacted_fields(doctype):
	s = get_settings()
	return {row.fieldname for row in (s.redacted_fields or []) if row.document_type == doctype}


def is_sensitive(df):
	if df.fieldtype == "Password":
		return True
	name = (df.fieldname or "").lower()
	return name in SENSITIVE_FIELDNAMES or "password" in name or "secret" in name


def assert_doctype_exists(doctype):
	if not doctype or not isinstance(doctype, str):
		raise ToolError(_("A DocType name is required"))
	if not frappe.db.exists("DocType", doctype):
		raise ToolError(
			_("DocType '{0}' does not exist. Use search_doctypes to find the correct name.").format(doctype)
		)


def assert_doctype_allowed(doctype):
	assert_doctype_exists(doctype)
	if doctype in blocked_doctypes():
		raise ToolError(_("The assistant is not allowed to access {0}.").format(doctype))


def assert_can_read(doctype, doc=None):
	assert_doctype_allowed(doctype)
	meta = frappe.get_meta(doctype)
	if meta.istable:
		raise ToolError(
			_("{0} is a child table. Read it through its parent document, or filter the parent list with it.").format(
				doctype
			)
		)
	if not frappe.has_permission(doctype, "read", doc=doc):
		label = _("this {0}").format(doctype) if doc else doctype
		raise ToolError(
			_("Permission denied: user {0} cannot read {1}.").format(frappe.session.user, label)
		)


def readable_fieldnames(meta, parenttype=None):
	"""Fieldnames (data fields only) the session user may read in this DocType."""
	permitted = None
	try:
		permitted = set(
			meta.get_permitted_fieldnames(parenttype=parenttype, permission_type="read")
		)
	except TypeError:
		try:
			permitted = set(meta.get_permitted_fieldnames(parenttype=parenttype))
		except Exception:
			permitted = None
	except Exception:
		permitted = None

	check_meta = frappe.get_meta(parenttype) if (meta.istable and parenttype) else meta
	levels = set(check_meta.get_permlevel_access("read") or []) | {0}
	if not permitted:
		# fallback: honour permlevel by hand
		permitted = {df.fieldname for df in meta.fields if cint(df.permlevel) in levels}
	# Frappe's helper leaves out child-table fields; add them back under the same permlevel rule
	permitted |= {df.fieldname for df in meta.fields if df.fieldtype in TABLE_TYPES and cint(df.permlevel) in levels}

	redacted = redacted_fields(meta.name)
	out = set()
	for df in meta.fields:
		if df.fieldname not in permitted or df.fieldtype in NO_VALUE_TYPES:
			continue
		if is_sensitive(df) or df.fieldname in redacted:
			continue
		out.add(df.fieldname)
	return out


def clean_doc_dict(meta, data, parenttype=None, include_empty=False):
	"""Strip a document dict to what the model may see, compactly."""
	allowed = readable_fieldnames(meta, parenttype=parenttype)
	std = STANDARD_FIELDS | (CHILD_STANDARD_FIELDS if meta.istable else set())
	out = {}
	for key, value in data.items():
		if key in NOISE_KEYS:
			continue
		if meta.istable and key in CHILD_STANDARD_FIELDS | {"owner", "creation", "modified", "modified_by", "docstatus", "name"}:
			continue
		if key not in allowed and key not in std and key != "_assign":
			continue
		df = meta.get_field(key)
		if df and df.fieldtype in TABLE_TYPES:
			rows = value or []
			child_meta = frappe.get_meta(df.options)
			cleaned = [
				clean_doc_dict(child_meta, r.as_dict() if isinstance(r, Document) else dict(r), parenttype=meta.name)
				for r in rows[:MAX_CHILD_ROWS]
			]
			if cleaned or include_empty:
				out[key] = cleaned
			if len(rows) > MAX_CHILD_ROWS:
				out[key + "__note"] = f"showing first {MAX_CHILD_ROWS} of {len(rows)} rows"
			continue
		if not include_empty and value in (None, "", [], {}):
			continue
		out[key] = compact_value(value)
	return out


def compact_value(value):
	if isinstance(value, str):
		if len(value) > MAX_TEXT:
			return value[:MAX_TEXT] + f"... [truncated, {len(value)} chars]"
		return value
	if isinstance(value, (int, float, bool)) or value is None:
		return value
	if isinstance(value, (list, tuple)):
		return [compact_value(v) for v in value]
	if isinstance(value, dict):
		return {k: compact_value(v) for k, v in value.items()}
	return str(value)  # dates, decimals, etc.


def valid_query_fieldnames(meta):
	"""Fieldnames that may appear in filters / fields / group by."""
	return readable_fieldnames(meta) | STANDARD_FIELDS
