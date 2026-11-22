"""Safe, permission-aware query helpers used by the read tools."""

import json

import frappe
from frappe import _
from frappe.utils import cint

from fibersoft_ai.ai.guard import (
	NUMERIC_TYPES,
	STANDARD_FIELDS,
	TABLE_TYPES,
	ToolError,
	assert_can_read,
	readable_fieldnames,
	valid_query_fieldnames,
)

OPERATORS = {
	"=", "!=", "<", ">", "<=", ">=", "like", "not like", "in", "not in", "between",
	"is", "descendants of", "ancestors of", "not descendants of", "not ancestors of", "timespan",
}
DATE_TYPES = {"Date", "Datetime"}


def slug(doctype):
	return doctype.lower().replace(" ", "-")


def doc_url(doctype, name):
	return f"/app/{slug(doctype)}/{name}"


def parse_jsonish(value, default=None):
	if value in (None, ""):
		return default
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			raise ToolError(_("Could not parse JSON: {0}").format(value[:200]))
	return value


def _check_field(meta, fieldname, purpose="filter"):
	if fieldname not in valid_query_fieldnames(meta):
		similar = sorted(
			f for f in valid_query_fieldnames(meta) if fieldname.lower().split("_")[0] in f.lower()
		)[:8]
		hint = _(" Similar fields: {0}.").format(", ".join(similar)) if similar else ""
		raise ToolError(
			_("'{0}' is not a readable field of {1} (cannot {2} on it).{3} Use describe_doctype to see fields.").format(
				fieldname, meta.name, purpose, hint
			)
		)


def normalize_filters(meta, filters):
	"""Accepts dict or list filters; returns validated list of [doctype, field, op, value]."""
	filters = parse_jsonish(filters, default=[])
	out = []
	if isinstance(filters, dict):
		items = []
		for key, val in filters.items():
			if isinstance(val, (list, tuple)) and len(val) == 2 and str(val[0]).lower() in OPERATORS:
				items.append([key, val[0], val[1]])
			else:
				items.append([key, "=", val])
		filters = items
	if not isinstance(filters, list):
		raise ToolError(_("filters must be a list like [[\"field\", \"=\", \"value\"]] or an object"))

	table_fields = {df.options: df for df in meta.get_table_fields()}
	for f in filters:
		if not isinstance(f, (list, tuple)) or len(f) not in (3, 4):
			raise ToolError(_("Each filter must be [field, operator, value] or [child_doctype, field, operator, value]. Got: {0}").format(f))
		if len(f) == 4:
			dt, field, op, value = f
		else:
			dt, (field, op, value) = meta.name, f
		op = str(op).lower().strip()
		if op not in OPERATORS:
			raise ToolError(_("Unsupported operator '{0}'. Use one of: {1}").format(op, ", ".join(sorted(OPERATORS))))
		if dt == meta.name:
			_check_field(meta, field)
		elif dt in table_fields:
			child_meta = frappe.get_meta(dt)
			if field not in readable_fieldnames(child_meta, parenttype=meta.name) | {"name", "idx"}:
				raise ToolError(_("'{0}' is not a readable field of child table {1}").format(field, dt))
		else:
			raise ToolError(
				_("{0} is not a child table of {1}. Child tables: {2}").format(dt, meta.name, ", ".join(table_fields) or "none")
			)
		if op in ("in", "not in") and isinstance(value, str):
			value = [v.strip() for v in value.split(",") if v.strip()]
		if op == "between" and (not isinstance(value, (list, tuple)) or len(value) != 2):
			raise ToolError(_("'between' needs a two-item list, e.g. [\"2026-01-01\", \"2026-01-31\"]"))
		if op == "is" and value not in ("set", "not set"):
			raise ToolError(_("'is' operator value must be 'set' or 'not set'"))
		out.append([dt, field, op, value])
	return out


def has_docstatus_filter(filters):
	return any(f[1] == "docstatus" for f in filters)


def default_status_filter(meta, filters, include_cancelled=False):
	"""Excludes cancelled documents unless the caller asked otherwise. Returns a note or None."""
	if meta.is_submittable and not include_cancelled and not has_docstatus_filter(filters):
		filters.append([meta.name, "docstatus", "!=", 2])
		return "Cancelled documents (docstatus=2) are excluded. Drafts (0) and submitted (1) are included unless you filter docstatus."
	return None


def validate_order_by(meta, order_by, allowed_aliases=()):
	if not order_by:
		return None
	parts = []
	for chunk in str(order_by).split(","):
		bits = chunk.strip().replace("`", "").split()
		if not bits:
			continue
		field = bits[0]
		direction = bits[1].lower() if len(bits) > 1 else "asc"
		if direction not in ("asc", "desc") or len(bits) > 2:
			raise ToolError(_("order_by must look like 'fieldname desc'"))
		if field in allowed_aliases:
			parts.append(f"{field} {direction}")
			continue
		_check_field(meta, field, "sort")
		parts.append(f"`tab{meta.name}`.`{field}` {direction}")
	return ", ".join(parts) or None


def run_get_list(doctype, **kwargs):
	"""frappe.get_list with DB errors turned into model-readable messages."""
	try:
		return frappe.get_list(doctype, **kwargs)
	except frappe.PermissionError:
		raise ToolError(_("Permission denied: user {0} cannot read {1}.").format(frappe.session.user, doctype))
	except (frappe.db.OperationalError, frappe.db.ProgrammingError) as e:
		raise ToolError(_("Query failed: {0}").format(str(e)[:400]))
	except frappe.DataError as e:
		raise ToolError(_("Query rejected: {0}").format(str(e)[:400]))


def count(doctype, filters):
	rows = run_get_list(
		doctype,
		filters=filters,
		fields=[f"count(distinct `tab{doctype}`.`name`) as total"],
		limit_page_length=0,
		order_by=None,
	)
	return cint(rows[0].get("total")) if rows else 0


def default_list_fields(meta):
	readable = readable_fieldnames(meta)
	fields = ["name"]
	for candidate in [meta.title_field, "status", "workflow_state"]:
		if candidate and candidate in readable and candidate not in fields:
			fields.append(candidate)
	for df in meta.fields:
		if df.in_list_view and df.fieldname in readable and df.fieldtype not in TABLE_TYPES and df.fieldname not in fields:
			fields.append(df.fieldname)
	for df in meta.fields:
		if df.in_standard_filter and df.fieldname in readable and df.fieldtype not in TABLE_TYPES and df.fieldname not in fields:
			fields.append(df.fieldname)
	if meta.is_submittable:
		fields.append("docstatus")
	fields.append("modified")
	return fields[:20]


def validate_fields(meta, fields):
	fields = parse_jsonish(fields, default=None)
	if not fields:
		return default_list_fields(meta)
	if isinstance(fields, str):
		fields = [f.strip() for f in fields.split(",")]
	out = []
	for f in fields:
		f = str(f).strip().replace("`", "")
		df = meta.get_field(f)
		if df and df.fieldtype in TABLE_TYPES:
			raise ToolError(_("'{0}' is a child table; use get_document to read its rows, or filter with [child_doctype, field, op, value].").format(f))
		_check_field(meta, f, "select")
		if f not in out:
			out.append(f)
	if "name" not in out:
		out.insert(0, "name")
	return out[:40]


def numeric_field(meta, fieldname):
	if fieldname in ("name",):
		return False
	df = meta.get_field(fieldname)
	return bool(df and df.fieldtype in NUMERIC_TYPES)


def field_type(meta, fieldname):
	if fieldname in ("creation", "modified"):
		return "Datetime"
	if fieldname in STANDARD_FIELDS:
		return "Data"
	df = meta.get_field(fieldname)
	return df.fieldtype if df else None


def prepare(doctype):
	assert_can_read(doctype)
	return frappe.get_meta(doctype)
