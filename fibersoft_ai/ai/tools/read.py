"""Read-only tools: structure, records, lists, totals, history, balances, stock."""

import json
from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, strip_html

from fibersoft_ai.ai.guard import (
	TABLE_TYPES,
	ToolError,
	assert_can_read,
	assert_doctype_allowed,
	blocked_doctypes,
	clean_doc_dict,
	compact_value,
	readable_fieldnames,
)
from fibersoft_ai.ai.tools import tool
from fibersoft_ai.ai.tools.query import (
	DATE_TYPES,
	count,
	default_status_filter,
	doc_url,
	field_type,
	normalize_filters,
	numeric_field,
	prepare,
	run_get_list,
	slug,
	validate_fields,
	validate_order_by,
	_check_field,
)

FILTERS_SCHEMA = {
	"description": (
		'Filters as a list of [field, operator, value] (or [child_doctype, field, operator, value] to filter by a '
		'child-table field), e.g. [["customer","=","Ali Traders"],["posting_date","between",["2026-11-01","2026-11-30"]]]. '
		"Operators: =, !=, <, >, <=, >=, like, not like, in, not in, between, is (value 'set'/'not set'), timespan "
		"(value 'today', 'this week', 'last month', 'this year'...). An object {field: value} is also accepted."
	),
	"anyOf": [{"type": "array"}, {"type": "object"}],
}
INCLUDE_CANCELLED = {
	"type": "boolean",
	"description": "Include cancelled documents. Default false (cancelled are excluded unless you filter docstatus yourself).",
}


# ----------------------------------------------------------------------------- structure
@tool(
	"describe_doctype",
	"""Get the structure of a DocType: its fields (name, label, type, options), child tables, whether it is
submittable, the user's permissions on it, its workflow (states and transitions) and the reports built on it.
Call this before filtering or aggregating on fields you are not sure about, and before proposing actions.""",
	{
		"doctype": {"type": "string", "description": "Exact DocType name, e.g. 'Sales Invoice'"},
		"include_child_tables": {"type": "boolean", "description": "Also describe child table fields. Default true."},
	},
	["doctype"],
)
def describe_doctype(ctx, doctype, include_child_tables=True):
	assert_doctype_allowed(doctype)
	meta = frappe.get_meta(doctype)
	if not meta.istable and not frappe.has_permission(doctype, "read"):
		raise ToolError(_("Permission denied: user {0} cannot read {1}.").format(frappe.session.user, doctype))

	def field_list(m, parenttype=None):
		readable = readable_fieldnames(m, parenttype=parenttype)
		rows = []
		for df in m.fields:
			if df.fieldname not in readable:
				continue
			row = {"fieldname": df.fieldname, "label": df.label, "type": df.fieldtype}
			if df.options and df.fieldtype in ("Link", "Dynamic Link", "Table", "Table MultiSelect"):
				row["options"] = df.options
			elif df.options and df.fieldtype == "Select":
				opts = [o for o in df.options.split("\n") if o.strip()]
				row["options"] = opts[:40]
			if df.reqd:
				row["reqd"] = 1
			if df.read_only:
				row["read_only"] = 1
			if df.allow_on_submit:
				row["allow_on_submit"] = 1
			if df.default:
				row["default"] = df.default
			if df.fetch_from:
				row["fetch_from"] = df.fetch_from
			if df.get("is_custom_field"):
				row["custom"] = 1
			rows.append(row)
		return rows

	out = OrderedDict(
		doctype=doctype,
		module=meta.module,
		is_submittable=bool(meta.is_submittable),
		is_child_table=bool(meta.istable),
		is_single=bool(meta.issingle),
		title_field=meta.title_field,
		naming=meta.autoname,
		url=f"/app/{slug(doctype)}",
		fields=field_list(meta),
	)
	if meta.is_submittable:
		out["docstatus_meaning"] = {"0": "Draft", "1": "Submitted", "2": "Cancelled"}
	if include_child_tables:
		children = {}
		for df in meta.get_table_fields():
			if df.fieldname in readable_fieldnames(meta):
				cm = frappe.get_meta(df.options)
				children[df.fieldname] = {"doctype": df.options, "fields": field_list(cm, parenttype=doctype)}
		if children:
			out["child_tables"] = children
	if not meta.istable:
		out["your_permissions"] = {
			p: bool(frappe.has_permission(doctype, p))
			for p in ("read", "write", "create", "delete", "submit", "cancel", "report")
		}
		wf = _workflow(doctype)
		if wf:
			out["workflow"] = wf
		reports = frappe.get_all(
			"Report",
			filters={"ref_doctype": doctype, "disabled": 0},
			fields=["name", "report_type"],
			order_by="name asc",
			limit_page_length=40,
		)
		if reports:
			out["reports"] = reports
	return out


def _workflow(doctype):
	name = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
	if not name:
		return None
	wf = frappe.get_cached_doc("Workflow", name)
	return {
		"name": name,
		"state_field": wf.workflow_state_field,
		"states": [{"state": s.state, "docstatus": cint(s.doc_status), "allow_edit": s.allow_edit} for s in wf.states],
		"transitions": [
			{"from": t.state, "action": t.action, "to": t.next_state, "allowed_role": t.allowed, "condition": t.condition}
			for t in wf.transitions
		],
	}


@tool(
	"search_doctypes",
	"""Find DocTypes, reports and fields by keyword when you don't know the exact name. Searches DocType names,
report names, and field labels (including custom fields). E.g. 'imei', 'courier', 'ageing', 'carton'.""",
	{"text": {"type": "string"}, "limit": {"type": "integer", "description": "Default 15"}},
	["text"],
)
def search_doctypes(ctx, text, limit=15):
	text = (text or "").strip()
	if len(text) < 2:
		raise ToolError(_("Search text must be at least 2 characters"))
	limit = min(max(cint(limit) or 15, 1), 50)
	like = f"%{text}%"
	blocked = blocked_doctypes()

	doctypes = [
		d
		for d in frappe.get_all(
			"DocType",
			filters={"istable": 0},
			or_filters=[["name", "like", like], ["module", "like", like]],
			fields=["name", "module", "is_submittable"],
			limit_page_length=limit * 3,
			order_by="name asc",
		)
		if d.name not in blocked and frappe.has_permission(d.name, "read")
	][:limit]

	reports = []
	for r in frappe.get_all(
		"Report",
		filters={"disabled": 0, "name": ["like", like]},
		fields=["name", "report_type", "ref_doctype", "is_standard"],
		limit_page_length=limit * 3,
		order_by="name asc",
	):
		try:
			if frappe.get_cached_doc("Report", r.name).is_permitted():
				reports.append(r)
		except Exception:
			continue
		if len(reports) >= limit:
			break

	fields = []
	for src in ("DocField", "Custom Field"):
		parent_key = "parent" if src == "DocField" else "dt"
		rows = frappe.get_all(
			src,
			filters={"label": ["like", like], "fieldtype": ["not in", ["Section Break", "Column Break", "Tab Break"]]},
			fields=[f"{parent_key} as doctype", "fieldname", "label", "fieldtype"],
			limit_page_length=limit * 3,
		)
		for r in rows:
			if r.doctype in blocked:
				continue
			m = frappe.get_meta(r.doctype)
			if m.istable:
				parent = frappe.db.get_value("DocField", {"options": r.doctype, "fieldtype": ["in", list(TABLE_TYPES)]}, "parent")
				if not parent or not frappe.has_permission(parent, "read"):
					continue
				r["child_table_of"] = parent
			elif not frappe.has_permission(r.doctype, "read"):
				continue
			fields.append(r)
	return {"doctypes": doctypes, "reports": reports, "fields": fields[:limit]}


# ----------------------------------------------------------------------------- records
@tool(
	"find_records",
	"""Find records of a DocType by (partial) name, title or search fields — use this to resolve what the user
typed (e.g. a customer or item name) into the exact record name before filtering or proposing actions.""",
	{
		"doctype": {"type": "string"},
		"text": {"type": "string", "description": "Partial name / title / code"},
		"limit": {"type": "integer", "description": "Default 10, max 30"},
	},
	["doctype", "text"],
)
def find_records(ctx, doctype, text, limit=10):
	meta = prepare(doctype)
	limit = min(max(cint(limit) or 10, 1), 30)
	results = []
	try:
		from frappe.desk.search import search_widget

		rows = search_widget(doctype, txt=text or "", page_length=limit, as_dict=True) or []
		for r in rows:
			r = dict(r)
			name = r.pop("name", None) or r.pop("value", None)
			if not name:
				continue
			desc = ", ".join(str(v) for v in r.values() if v not in (None, "") and str(v) != str(name))
			results.append({"name": name, "description": desc[:200] or None, "url": doc_url(doctype, name)})
	except Exception:
		results = []
	if not results:
		or_filters = [["name", "like", f"%{text}%"]]
		readable = readable_fieldnames(meta)
		for f in [meta.title_field] + (meta.search_fields or "").split(","):
			f = (f or "").strip()
			if f and f in readable:
				or_filters.append([f, "like", f"%{text}%"])
		fields = ["name"] + ([meta.title_field] if meta.title_field in readable else [])
		for r in run_get_list(doctype, or_filters=or_filters, fields=fields, limit_page_length=limit):
			results.append({"name": r.name, "title": r.get(meta.title_field), "url": doc_url(doctype, r.name)})
	return {"doctype": doctype, "matches": results, "exact_match": any(str(r["name"]).lower() == str(text).lower() for r in results)}


@tool(
	"get_document",
	"""Read one complete document (all readable fields and child-table rows) the user is permitted to see.
docstatus: 0 Draft, 1 Submitted, 2 Cancelled.""",
	{
		"doctype": {"type": "string"},
		"name": {"type": "string", "description": "Exact document name (ID). Use find_records if unsure."},
		"fields": {"type": "array", "items": {"type": "string"}, "description": "Optional: only these top-level fields"},
	},
	["doctype", "name"],
)
def get_document(ctx, doctype, name, fields=None):
	assert_doctype_allowed(doctype)
	meta = frappe.get_meta(doctype)
	if meta.issingle:
		name = doctype
	if not frappe.db.exists(doctype, name):
		raise ToolError(_("{0} '{1}' not found. Use find_records to search.").format(doctype, name))
	doc = frappe.get_doc(doctype, name)
	assert_can_read(doctype, doc=doc)
	doc.apply_fieldlevel_read_permissions()
	data = clean_doc_dict(meta, doc.as_dict())
	if fields:
		keep = set(fields) | {"name", "docstatus"}
		data = {k: v for k, v in data.items() if k in keep or k.split("__")[0] in keep}
	if data.get("_assign"):
		try:
			data["assigned_to"] = json.loads(data.pop("_assign"))
		except Exception:
			data.pop("_assign", None)
	else:
		data.pop("_assign", None)
	data["url"] = doc_url(doctype, doc.name)
	return {"doctype": doctype, "document": data}


@tool(
	"list_documents",
	"""List records with filters, chosen fields, sorting and paging (permission-aware). Returns rows plus the total
number of matching records. For totals/sums/averages/group-by use the aggregate tool instead of adding rows up.""",
	{
		"doctype": {"type": "string"},
		"filters": FILTERS_SCHEMA,
		"fields": {"type": "array", "items": {"type": "string"}, "description": "Fieldnames; default = list-view fields"},
		"order_by": {"type": "string", "description": "e.g. 'posting_date desc'"},
		"limit": {"type": "integer", "description": "Rows to return, default 20, max 500"},
		"offset": {"type": "integer"},
		"include_cancelled": INCLUDE_CANCELLED,
	},
	["doctype"],
)
def list_documents(ctx, doctype, filters=None, fields=None, order_by=None, limit=20, offset=0, include_cancelled=False):
	meta = prepare(doctype)
	flt_list = normalize_filters(meta, filters)
	note = default_status_filter(meta, flt_list, include_cancelled)
	field_list = validate_fields(meta, fields)
	limit = min(max(cint(limit) or 20, 1), 500)
	rows = run_get_list(
		doctype,
		filters=flt_list,
		fields=field_list,
		order_by=validate_order_by(meta, order_by) or f"`tab{doctype}`.`modified` desc",
		limit_start=max(cint(offset), 0),
		limit_page_length=limit,
		distinct=True,
	)
	total = count(doctype, flt_list)
	out = {
		"doctype": doctype,
		"total_matching": total,
		"returned": len(rows),
		"offset": cint(offset),
		"rows": [{k: compact_value(v) for k, v in r.items()} for r in rows],
		"url_pattern": f"/app/{slug(doctype)}/<name>",
		"applied_filters": flt_list,
	}
	if total > len(rows) + cint(offset):
		out["more"] = f"{total - len(rows) - cint(offset)} more records match; use offset/limit or aggregate."
	if note:
		out["note"] = note
	return out


@tool(
	"count_documents",
	"Count records matching filters (permission-aware).",
	{"doctype": {"type": "string"}, "filters": FILTERS_SCHEMA, "include_cancelled": INCLUDE_CANCELLED},
	["doctype"],
)
def count_documents(ctx, doctype, filters=None, include_cancelled=False):
	meta = prepare(doctype)
	flt_list = normalize_filters(meta, filters)
	note = default_status_filter(meta, flt_list, include_cancelled)
	out = {"doctype": doctype, "count": count(doctype, flt_list), "applied_filters": flt_list}
	if note:
		out["note"] = note
	return out


PERIODS = ("day", "week", "month", "quarter", "year")


@tool(
	"aggregate",
	"""Exact totals computed by the database: sum / count / avg / min / max of fields, optionally grouped by up to
two fields, and optionally bucketed by a date field into day/week/month/quarter/year. Use this for any
'total', 'how much', 'top N', 'per customer', 'monthly trend' question — never add numbers up yourself.""",
	{
		"doctype": {"type": "string"},
		"metrics": {
			"type": "array",
			"description": 'e.g. [{"function":"sum","field":"grand_total"},{"function":"count"}]',
			"items": {
				"type": "object",
				"properties": {
					"function": {"type": "string", "enum": ["sum", "count", "avg", "min", "max"]},
					"field": {"type": "string", "description": "Numeric field (not needed for count)"},
				},
				"required": ["function"],
			},
		},
		"group_by": {"type": "array", "items": {"type": "string"}, "description": "Up to 2 fieldnames"},
		"date_field": {"type": "string", "description": "Date field to bucket by, e.g. posting_date"},
		"period": {"type": "string", "enum": list(PERIODS)},
		"filters": FILTERS_SCHEMA,
		"order_by": {"type": "string", "description": "'value desc' (first metric), 'value_2 asc', or a group field"},
		"limit": {"type": "integer", "description": "Max groups returned, default 50, max 500"},
		"include_cancelled": INCLUDE_CANCELLED,
	},
	["doctype", "metrics"],
)
def aggregate(
	ctx, doctype, metrics, group_by=None, date_field=None, period=None, filters=None, order_by=None, limit=50,
	include_cancelled=False,
):
	meta = prepare(doctype)
	flt_list = normalize_filters(meta, filters)
	note = default_status_filter(meta, flt_list, include_cancelled)
	limit = min(max(cint(limit) or 50, 1), 500)

	if isinstance(metrics, dict):
		metrics = [metrics]
	if not metrics or not isinstance(metrics, list):
		raise ToolError(_("metrics must be a non-empty list"))
	if isinstance(group_by, str):
		group_by = [g.strip() for g in group_by.split(",") if g.strip()]
	group_by = list(group_by or [])
	if len(group_by) > 2:
		raise ToolError(_("At most 2 group_by fields"))
	for g in group_by:
		_check_field(meta, g, "group by")
		if meta.get_field(g) and meta.get_field(g).fieldtype in TABLE_TYPES:
			raise ToolError(_("Cannot group by a child table field"))

	if period and not date_field:
		raise ToolError(_("period needs date_field"))
	if date_field:
		_check_field(meta, date_field, "bucket")
		if field_type(meta, date_field) not in DATE_TYPES:
			raise ToolError(_("{0} is not a Date/Datetime field").format(date_field))
		period = period or "month"
		if period not in PERIODS:
			raise ToolError(_("period must be one of {0}").format(", ".join(PERIODS)))

	sql_fields, metric_keys = [], []
	for i, m in enumerate(metrics):
		fn = str(m.get("function", "")).lower()
		field = m.get("field")
		key = "value" if i == 0 else f"value_{i + 1}"
		if fn == "count":
			sql_fields.append(f"count(distinct `tab{doctype}`.`name`) as {key}")
		elif fn in ("sum", "avg", "min", "max"):
			if not field:
				raise ToolError(_("{0} needs a field").format(fn))
			_check_field(meta, field, fn)
			if fn in ("sum", "avg") and not numeric_field(meta, field):
				raise ToolError(_("{0} is not numeric; cannot {1}").format(field, fn))
			if fn == "avg":
				# keep sum + count so buckets roll up exactly
				sql_fields.append(f"sum(`tab{doctype}`.`{field}`) as {key}__sum")
				sql_fields.append(f"count(`tab{doctype}`.`{field}`) as {key}__n")
			else:
				sql_fields.append(f"{fn}(`tab{doctype}`.`{field}`) as {key}")
		else:
			raise ToolError(_("Unknown function {0}").format(fn))
		metric_keys.append((key, fn, field))

	group_cols = list(group_by) + ([date_field] if date_field else [])
	select_groups = [f"`tab{doctype}`.`{g}` as `{g}`" for g in group_cols]
	# count(distinct name) already protects counts against child-table join duplication
	query_filters = flt_list
	if any(f[0] != doctype for f in flt_list):
		# Child-table filters join child rows and would multiply parent sums. Resolve the matching
		# parents first, then aggregate over those parents only - exact totals.
		names = run_get_list(doctype, filters=flt_list, fields=["name"], distinct=True, limit_page_length=50001, order_by=None)
		if len(names) > 50000:
			raise ToolError(_("Too many matching documents for a child-table filter; add a date range."))
		query_filters = [f for f in flt_list if f[0] == doctype] + [[doctype, "name", "in", [n.name for n in names] or ["__none__"]]]

	rows = run_get_list(
		doctype,
		filters=query_filters,
		fields=select_groups + sql_fields,
		group_by=", ".join(f"`tab{doctype}`.`{g}`" for g in group_cols) or None,
		order_by=None,
		limit_page_length=0 if group_cols else 1,
	)

	# --- bucket dates and finish averages in Python (exact, no SQL date functions needed)
	buckets = OrderedDict()
	for r in rows:
		key_vals = [r.get(g) for g in group_by]
		if date_field:
			key_vals.append(_bucket(r.get(date_field), period))
		k = tuple(str(v) if v is not None else None for v in key_vals)
		agg = buckets.setdefault(k, {"_keys": key_vals})
		for key, fn, _field in metric_keys:
			if fn == "avg":
				agg[key + "__sum"] = flt(agg.get(key + "__sum")) + flt(r.get(key + "__sum"))
				agg[key + "__n"] = cint(agg.get(key + "__n")) + cint(r.get(key + "__n"))
			elif fn in ("sum", "count"):
				agg[key] = flt(agg.get(key)) + flt(r.get(key))
			elif fn == "min":
				v = r.get(key)
				if v is not None and (agg.get(key) is None or v < agg[key]):
					agg[key] = v
			elif fn == "max":
				v = r.get(key)
				if v is not None and (agg.get(key) is None or v > agg[key]):
					agg[key] = v

	labels = list(group_by) + (["period"] if date_field else [])
	result = []
	for agg in buckets.values():
		row = OrderedDict((labels[i], compact_value(v)) for i, v in enumerate(agg["_keys"]))
		for key, fn, _field in metric_keys:
			if fn == "avg":
				n = agg.get(key + "__n") or 0
				row[key] = round(agg.get(key + "__sum", 0) / n, 6) if n else None
			elif fn == "count":
				row[key] = int(agg.get(key) or 0)
			elif fn == "sum":
				row[key] = round(flt(agg.get(key)), 6)
			else:
				row[key] = compact_value(agg.get(key))
		result.append(row)

	# order
	if order_by:
		bits = str(order_by).split()
		ob_field = bits[0]
		desc = len(bits) > 1 and bits[1].lower() == "desc"
		valid = labels + [k for k, _f, _x in metric_keys]
		if ob_field not in valid:
			raise ToolError(_("order_by must be one of: {0}").format(", ".join(valid)))
		present = [r for r in result if r.get(ob_field) is not None]
		missing = [r for r in result if r.get(ob_field) is None]
		try:
			present.sort(key=lambda r: r[ob_field], reverse=desc)
		except TypeError:
			present.sort(key=lambda r: str(r[ob_field]), reverse=desc)
		result = present + missing
	elif date_field:
		result.sort(key=lambda r: str(r.get("period")))

	total_groups = len(result)
	grand = {}
	for key, fn, _field in metric_keys:
		if fn == "count":
			grand[key] = int(sum(cint(r.get(key)) for r in result))
		elif fn == "sum":
			grand[key] = round(sum(flt(r.get(key)) for r in result), 6)
	out = {
		"doctype": doctype,
		"metrics": [{"key": k, "function": fn, "field": f} for k, fn, f in metric_keys],
		"groups": result[:limit],
		"total_groups": total_groups,
		"applied_filters": flt_list,
	}
	if group_cols:
		out["grand_total_all_groups"] = grand
	if total_groups > limit:
		out["more"] = f"Showing {limit} of {total_groups} groups (grand totals cover all groups)."
	if note:
		out["note"] = note
	return out


def _bucket(value, period):
	if value in (None, ""):
		return None
	d = getdate(value)
	if period == "day":
		return d.isoformat()
	if period == "week":
		iso = d.isocalendar()
		return f"{iso[0]}-W{iso[1]:02d}"
	if period == "month":
		return f"{d.year}-{d.month:02d}"
	if period == "quarter":
		return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
	return str(d.year)


# ----------------------------------------------------------------------------- history
@tool(
	"get_document_activity",
	"""Who created / changed / commented on a document and when: field changes (old → new), child-row changes,
comments, workflow actions and assignments. Use for 'who changed this', 'why was this cancelled', etc.""",
	{"doctype": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "description": "Default 25"}},
	["doctype", "name"],
)
def get_document_activity(ctx, doctype, name, limit=25):
	assert_doctype_allowed(doctype)
	if not frappe.db.exists(doctype, name):
		raise ToolError(_("{0} '{1}' not found").format(doctype, name))
	doc = frappe.get_doc(doctype, name)
	assert_can_read(doctype, doc=doc)
	meta = frappe.get_meta(doctype)
	readable = readable_fieldnames(meta)
	limit = min(max(cint(limit) or 25, 1), 100)

	changes = []
	for v in frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": name},
		fields=["owner", "creation", "data"],
		order_by="creation desc",
		limit_page_length=limit,
	):
		try:
			data = json.loads(v.data or "{}")
		except ValueError:
			continue
		entry = {"by": v.owner, "on": str(v.creation)}
		changed = [
			{"field": c[0], "label": meta.get_label(c[0]), "from": compact_value(c[1]), "to": compact_value(c[2])}
			for c in data.get("changed") or []
			if c and c[0] in readable | {"docstatus"}
		]
		if changed:
			entry["changed"] = changed
		for key in ("added", "removed"):
			if data.get(key):
				entry[f"rows_{key}"] = [f"{r[0]} row" for r in data[key]][:20]
		if data.get("row_changed"):
			entry["rows_changed"] = [
				{"table": r[0], "row": r[1], "changes": [[c[0], compact_value(c[1]), compact_value(c[2])] for c in r[3]]}
				for r in data["row_changed"][:20]
			]
		if data.get("comment"):
			entry["note"] = strip_html(str(data["comment"]))[:300]
		if len(entry) > 2:
			changes.append(entry)

	comments = [
		{"type": c.comment_type, "by": c.owner, "on": str(c.creation), "text": strip_html(c.content or "")[:500]}
		for c in frappe.get_all(
			"Comment",
			filters={"reference_doctype": doctype, "reference_name": name,
					 "comment_type": ["in", ["Comment", "Info", "Workflow", "Assigned", "Assignment Completed", "Attachment", "Shared", "Edit", "Cancelled", "Submitted"]]},
			fields=["comment_type", "owner", "creation", "content"],
			order_by="creation desc",
			limit_page_length=limit,
		)
	]
	return {
		"doctype": doctype,
		"name": name,
		"created_by": doc.owner,
		"created_on": str(doc.creation),
		"last_modified_by": doc.modified_by,
		"last_modified_on": str(doc.modified),
		"field_changes": changes,
		"comments_and_events": comments,
	}


# ----------------------------------------------------------------------------- accounting & stock
@tool(
	"get_balance",
	"""Exact ledger balance from posted GL entries (cancelled entries excluded): for a party (Customer, Supplier,
Employee...) and/or an account (group accounts include all sub-accounts), optionally per company and date range.
balance = debit - credit (positive for a Customer = they owe you; negative for a Supplier = you owe them).""",
	{
		"party_type": {"type": "string", "description": "Customer, Supplier, Employee, Shareholder..."},
		"party": {"type": "string"},
		"account": {"type": "string", "description": "Account name, e.g. 'Debtors - FS'"},
		"company": {"type": "string"},
		"from_date": {"type": "string", "description": "YYYY-MM-DD (gives opening + period movement)"},
		"to_date": {"type": "string", "description": "YYYY-MM-DD, default today"},
	},
)
def get_balance(ctx, party_type=None, party=None, account=None, company=None, from_date=None, to_date=None):
	if not frappe.db.exists("DocType", "GL Entry"):
		raise ToolError(_("Accounting is not installed on this site"))
	assert_can_read("GL Entry")
	if not (party or account):
		raise ToolError(_("Give a party (with party_type) or an account"))
	if party and not party_type:
		raise ToolError(_("party_type is required with party"))
	base = [["GL Entry", "is_cancelled", "=", 0]]
	if party:
		if party_type not in ("Customer", "Supplier", "Employee", "Shareholder", "Student", "Member", "Donor"):
			if not frappe.db.exists("DocType", party_type or ""):
				raise ToolError(_("Unknown party_type '{0}'. Use Customer, Supplier, Employee or Shareholder.").format(party_type))
		if not frappe.db.exists(party_type, party):
			raise ToolError(_("{0} '{1}' not found. Use find_records.").format(party_type, party))
		base += [["GL Entry", "party_type", "=", party_type], ["GL Entry", "party", "=", party]]
	if account:
		acc = frappe.db.get_value("Account", account, ["is_group", "lft", "rgt", "root_type", "account_currency"], as_dict=True)
		if not acc:
			raise ToolError(_("Account '{0}' not found. Use find_records on Account.").format(account))
		if acc.is_group:
			children = frappe.get_all("Account", filters={"lft": [">=", acc.lft], "rgt": ["<=", acc.rgt], "is_group": 0}, pluck="name")
			base.append(["GL Entry", "account", "in", children or [account]])
		else:
			base.append(["GL Entry", "account", "=", account])
	if company:
		base.append(["GL Entry", "company", "=", company])
	to_date = str(getdate(to_date)) if to_date else str(getdate())

	def sums(extra):
		r = run_get_list(
			"GL Entry",
			filters=base + extra,
			fields=["sum(`tabGL Entry`.`debit`) as debit", "sum(`tabGL Entry`.`credit`) as credit", "count(`tabGL Entry`.`name`) as entries"],
			limit_page_length=1,
			order_by=None,
		)
		r = r[0] if r else {}
		d, c = flt(r.get("debit"), 2), flt(r.get("credit"), 2)
		return {"debit": d, "credit": c, "balance": flt(d - c, 2), "entries": cint(r.get("entries"))}

	out = {"party_type": party_type, "party": party, "account": account, "company": company, "as_of": to_date}
	if from_date:
		from_date = str(getdate(from_date))
		out["from_date"] = from_date
		out["opening"] = sums([["GL Entry", "posting_date", "<", from_date]])
		out["period"] = sums([["GL Entry", "posting_date", "between", [from_date, to_date]]])
		out["closing_balance"] = flt(out["opening"]["balance"] + out["period"]["balance"], 2)
	else:
		out["closing"] = sums([["GL Entry", "posting_date", "<=", to_date]])
	out["currency_note"] = "Amounts are in company currency."
	return out


@tool(
	"get_stock_levels",
	"""Current stock per item and warehouse (actual, reserved, ordered, projected qty, valuation, stock value)
from the stock bin. Filter by item and/or warehouse.""",
	{
		"item_code": {"type": "string"},
		"warehouse": {"type": "string"},
		"only_in_stock": {"type": "boolean", "description": "Hide rows with zero actual qty. Default true."},
		"limit": {"type": "integer", "description": "Default 50"},
	},
)
def get_stock_levels(ctx, item_code=None, warehouse=None, only_in_stock=True, limit=50):
	if not frappe.db.exists("DocType", "Bin"):
		raise ToolError(_("Stock is not installed on this site"))
	assert_can_read("Bin")
	filters = []
	if item_code:
		filters.append(["Bin", "item_code", "=", item_code])
	if warehouse:
		wh = frappe.db.get_value("Warehouse", warehouse, ["is_group", "lft", "rgt"], as_dict=True)
		if not wh:
			raise ToolError(_("Warehouse '{0}' not found").format(warehouse))
		if wh.is_group:
			children = frappe.get_all("Warehouse", filters={"lft": [">=", wh.lft], "rgt": ["<=", wh.rgt]}, pluck="name")
			filters.append(["Bin", "warehouse", "in", children])
		else:
			filters.append(["Bin", "warehouse", "=", warehouse])
	if only_in_stock:
		filters.append(["Bin", "actual_qty", "!=", 0])
	limit = min(max(cint(limit) or 50, 1), 500)
	fields = ["item_code", "warehouse", "actual_qty", "reserved_qty", "ordered_qty", "indented_qty",
			  "projected_qty", "valuation_rate", "stock_value", "stock_uom"]
	readable = readable_fieldnames(frappe.get_meta("Bin")) | {"name"}
	fields = [f for f in fields if f in readable]
	rows = run_get_list("Bin", filters=filters, fields=fields, order_by="`tabBin`.`actual_qty` desc", limit_page_length=limit)
	tot = run_get_list(
		"Bin", filters=filters,
		fields=["sum(`tabBin`.`actual_qty`) as qty", "count(`tabBin`.`name`) as n"]
		+ (["sum(`tabBin`.`stock_value`) as value"] if "stock_value" in fields else []),
		limit_page_length=1, order_by=None,
	)[0]
	return {
		"rows": [{k: compact_value(v) for k, v in r.items()} for r in rows],
		"total_rows": cint(tot.get("n")),
		"total_actual_qty": flt(tot.get("qty"), 3),
		"total_stock_value": flt(tot.get("value"), 2) if "value" in tot else None,
		"note": "Total qty across different UOMs is only meaningful for a single item.",
	}
