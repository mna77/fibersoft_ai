"""Report tools: discover a report's filters and run any Query / Script / Report Builder report."""

import json
import os
import re

import frappe
from frappe import _
from frappe.utils import cint, flt, strip_html

from fibersoft_ai.ai.guard import ToolError, blocked_doctypes, compact_value
from fibersoft_ai.ai.tools import tool
from fibersoft_ai.ai.tools.query import normalize_filters, run_get_list, validate_order_by

SUMMABLE = {"Currency", "Float", "Int"}
MAX_ROWS = 300


def _get_report(report_name):
	if not report_name or not frappe.db.exists("Report", report_name):
		similar = frappe.get_all(
			"Report", filters={"name": ["like", f"%{(report_name or '')[:20]}%"], "disabled": 0}, pluck="name", limit=8
		)
		raise ToolError(
			_("Report '{0}' not found.{1}").format(
				report_name, (" " + _("Did you mean: {0}?").format(", ".join(similar))) if similar else " Use search_doctypes."
			)
		)
	report = frappe.get_doc("Report", report_name)
	if report.disabled:
		raise ToolError(_("Report '{0}' is disabled").format(report_name))
	if report.ref_doctype and report.ref_doctype in blocked_doctypes():
		raise ToolError(_("The assistant is not allowed to access {0}").format(report.ref_doctype))
	if not report.is_permitted():
		raise ToolError(_("Permission denied: user {0} cannot run report {1}").format(frappe.session.user, report_name))
	return report


def _report_js(report):
	if report.is_standard == "Yes" and report.module:
		try:
			module_path = frappe.get_module_path(report.module)
			path = os.path.join(module_path, "report", frappe.scrub(report.name), frappe.scrub(report.name) + ".js")
			if os.path.exists(path):
				with open(path, encoding="utf-8") as fh:
					return fh.read()
		except Exception:
			return ""
	return report.get("javascript") or ""


_FIELD_RE = re.compile(r"""fieldname\s*:\s*["'`]([A-Za-z0-9_]+)["'`]""")


def parse_js_filters(js):
	"""Best-effort extraction of filter definitions from a report's JS (fieldname/label/type/options/reqd/default)."""
	if not js:
		return []
	start = js.find("filters")
	body = js[start:] if start >= 0 else js
	matches = list(_FIELD_RE.finditer(body))
	out, seen = [], set()
	for i, m in enumerate(matches):
		chunk_start = body.rfind("{", 0, m.start())
		chunk_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(body), m.end() + 1200)
		chunk = body[chunk_start if chunk_start >= 0 else m.start(): chunk_end]
		fieldname = m.group(1)
		if fieldname in seen:
			continue
		seen.add(fieldname)

		def grab(key):
			mm = re.search(key + r"""\s*:\s*(?:__\()?\s*["'`]([^"'`]*)["'`]""", chunk)
			return mm.group(1) if mm else None

		item = {"fieldname": fieldname}
		for key in ("label", "fieldtype", "options"):
			v = grab(key)
			if v:
				item[key] = v.replace("\\n", "\n")
		if item.get("fieldtype") == "Select" and item.get("options"):
			item["options"] = [o for o in item["options"].split("\n") if o]
		if re.search(r"reqd\s*:\s*(1|true)", chunk):
			item["reqd"] = 1
		dm = re.search(r"default\s*:\s*([^,\n}]+)", chunk)
		if dm:
			item["default_js"] = dm.group(1).strip()[:120]
		out.append(item)
	return out[:40]


@tool(
	"get_report_info",
	"""Get a report's type, reference DocType and its filters (fieldname, label, type, options, required,
default) so you can run it correctly. Call before run_report unless the screen context already shows the
report's filters.""",
	{"report_name": {"type": "string"}},
	["report_name"],
)
def get_report_info(ctx, report_name):
	report = _get_report(report_name)
	filters = []
	for f in report.get("filters") or []:
		filters.append(
			{k: v for k, v in {"fieldname": f.fieldname, "label": f.label, "fieldtype": f.fieldtype,
							   "options": f.options, "reqd": cint(f.mandatory), "default": f.default}.items() if v}
		)
	if not filters:
		filters = parse_js_filters(_report_js(report))
	info = {
		"report_name": report.name,
		"report_type": report.report_type,
		"ref_doctype": report.ref_doctype,
		"is_standard": report.is_standard,
		"filters": filters,
		"url": f"/app/query-report/{report.name}" if report.report_type != "Report Builder" else None,
		"has_total_row": bool(report.add_total_row),
	}
	if report.report_type == "Report Builder":
		try:
			cfg = json.loads(report.json or "{}")
			info["saved_filters"] = cfg.get("filters")
			info["columns"] = cfg.get("columns") or cfg.get("fields")
		except ValueError:
			pass
	if any(f.get("fieldname") == "company" for f in filters):
		info["default_company"] = _default_company()
	return info


def _default_company():
	return frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")


def normalize_columns(columns):
	out = []
	for c in columns or []:
		if isinstance(c, dict):
			out.append({
				"fieldname": c.get("fieldname") or frappe.scrub(c.get("label") or ""),
				"label": strip_html(str(c.get("label") or c.get("fieldname") or "")),
				"fieldtype": c.get("fieldtype") or "Data",
				"options": c.get("options") if isinstance(c.get("options"), str) else None,
			})
		elif isinstance(c, str):
			parts = c.split(":")
			label = parts[0]
			ftype = parts[1] if len(parts) > 1 and parts[1] else "Data"
			options = None
			if "/" in ftype:
				ftype, options = ftype.split("/", 1)
			out.append({"fieldname": frappe.scrub(label), "label": label, "fieldtype": ftype, "options": options})
	return out


def rows_to_dicts(result, columns):
	names = [c["fieldname"] for c in columns]
	rows = []
	for r in result or []:
		if isinstance(r, dict):
			rows.append(r)
		elif isinstance(r, (list, tuple)):
			rows.append({names[i] if i < len(names) else f"col_{i}": v for i, v in enumerate(r)})
	return rows


_OPS = {
	"=": lambda a, b: str(a).lower() == str(b).lower() if not _num(a) or not _num(b) else flt(a) == flt(b),
	"!=": lambda a, b: not _OPS["="](a, b),
	">": lambda a, b: flt(a) > flt(b),
	"<": lambda a, b: flt(a) < flt(b),
	">=": lambda a, b: flt(a) >= flt(b),
	"<=": lambda a, b: flt(a) <= flt(b),
	"like": lambda a, b: str(b).strip("%").lower() in str(a or "").lower(),
	"not like": lambda a, b: str(b).strip("%").lower() not in str(a or "").lower(),
	"in": lambda a, b: str(a) in [str(x) for x in (b or [])],
	"not in": lambda a, b: str(a) not in [str(x) for x in (b or [])],
	"is set": lambda a, b: a not in (None, ""),
	"is not set": lambda a, b: a in (None, ""),
}


def _num(v):
	if isinstance(v, (int, float)):
		return True
	try:
		float(str(v))
		return True
	except ValueError:
		return False


@tool(
	"run_report",
	"""Run a report (Query, Script or Report Builder) with filters, exactly as the user would, and get its
columns, rows, exact column totals and summary. Large results can be narrowed server-side with where/search,
sorted with sort_by, and limited — use these for 'top 10', 'only overdue', etc. instead of reading every row.""",
	{
		"report_name": {"type": "string"},
		"filters": {"type": "object", "description": "Report filters as {fieldname: value}, e.g. {\"company\":\"X\",\"from_date\":\"2026-11-01\"}"},
		"where": {"type": "array", "description": 'Row filters on the result: [["column_fieldname", "op", value]]; ops = != > < >= <= like "not like" in "not in" "is set" "is not set"'},
		"search": {"type": "string", "description": "Keep rows where any cell contains this text"},
		"columns": {"type": "array", "items": {"type": "string"}, "description": "Only return these column fieldnames"},
		"sort_by": {"type": "string", "description": "Column fieldname to sort by"},
		"sort_desc": {"type": "boolean"},
		"limit": {"type": "integer", "description": "Rows to return, default 50, max 300. Totals always cover all matching rows."},
	},
	["report_name"],
)
def run_report(ctx, report_name, filters=None, where=None, search=None, columns=None, sort_by=None, sort_desc=False, limit=50):
	report = _get_report(report_name)
	filters = dict(filters or {}) if not isinstance(filters, str) else json.loads(filters or "{}")
	limit = min(max(cint(limit) or 50, 1), MAX_ROWS)
	auto = {}

	if report.report_type == "Report Builder":
		cols, rows, extra = _run_report_builder(report, filters)
	else:
		info_filters = [f.get("fieldname") for f in (get_report_info(ctx, report_name).get("filters") or [])]
		if "company" in info_filters and not filters.get("company") and _default_company():
			filters["company"] = auto["company"] = _default_company()
		from frappe.desk.query_report import run as run_query_report

		try:
			res = run_query_report(
				report.name, filters=filters, user=frappe.session.user, ignore_prepared_report=True,
				are_default_filters=False,
			)
		except frappe.PermissionError:
			raise ToolError(_("Permission denied running {0}").format(report.name))
		except (KeyError, TypeError, AttributeError) as e:
			raise ToolError(
				_("The report failed, most likely because a required filter is missing ({0}). Use get_report_info to see its filters.").format(e)
			)
		res = res or {}
		cols = normalize_columns(res.get("columns"))
		rows = rows_to_dicts(res.get("result"), cols)
		extra = {}
		if report.add_total_row and rows:
			extra["report_total_row"] = {k: compact_value(v) for k, v in rows[-1].items()}
			rows = rows[:-1]
		if res.get("report_summary"):
			extra["report_summary"] = [
				{"label": strip_html(str(s.get("label", ""))), "value": compact_value(s.get("value"))}
				for s in res["report_summary"] if isinstance(s, dict)
			]
		if res.get("message"):
			extra["report_message"] = strip_html(str(res["message"]))[:1000]

	# --- server-side narrowing
	colnames = {c["fieldname"] for c in cols}
	if where:
		where = json.loads(where) if isinstance(where, str) else where
		for cond in where:
			if not isinstance(cond, (list, tuple)) or len(cond) not in (2, 3):
				raise ToolError(_("where items must be [column, op, value]"))
			col, op = cond[0], str(cond[1]).lower()
			val = cond[2] if len(cond) == 3 else None
			if col not in colnames:
				raise ToolError(_("Unknown column '{0}'. Columns: {1}").format(col, ", ".join(sorted(colnames))))
			if op not in _OPS:
				raise ToolError(_("Unsupported op '{0}'").format(op))
			rows = [r for r in rows if _OPS[op](r.get(col), val)]
	if search:
		s = str(search).lower()
		rows = [r for r in rows if any(s in str(v).lower() for v in r.values() if v is not None)]

	tree = any(cint(r.get("indent")) > 0 for r in rows)
	totals = {}
	if not tree:
		for c in cols:
			if c["fieldtype"] in SUMMABLE:
				vals = [r.get(c["fieldname"]) for r in rows if isinstance(r.get(c["fieldname"]), (int, float))]
				if vals:
					totals[c["fieldname"]] = round(sum(flt(v) for v in vals), 6)

	if sort_by:
		if sort_by not in colnames:
			raise ToolError(_("Unknown sort column '{0}'").format(sort_by))
		present = [r for r in rows if r.get(sort_by) not in (None, "")]
		missing = [r for r in rows if r.get(sort_by) in (None, "")]
		try:
			present.sort(key=lambda r: r[sort_by], reverse=bool(sort_desc))
		except TypeError:
			present.sort(key=lambda r: str(r[sort_by]), reverse=bool(sort_desc))
		rows = present + missing

	keep = None
	if columns:
		keep = [c for c in columns if c in colnames]
		if not keep:
			raise ToolError(_("None of the requested columns exist. Columns: {0}").format(", ".join(sorted(colnames))))
		cols = [c for c in cols if c["fieldname"] in keep]

	out_rows = []
	for r in rows[:limit]:
		item = {}
		for k, v in r.items():
			if keep and k not in keep and k != "indent":
				continue
			if v in (None, ""):
				continue
			item[k] = compact_value(strip_html(v) if isinstance(v, str) and "<" in v else v)
		out_rows.append(item)

	out = {
		"report_name": report.name,
		"filters_used": filters,
		"columns": [{k: v for k, v in c.items() if v} for c in cols],
		"total_rows": len(rows),
		"returned_rows": len(out_rows),
		"rows": out_rows,
		"column_totals": totals,
		"url": f"/app/query-report/{report.name}" if report.report_type != "Report Builder" else f"/app/{report.ref_doctype.lower().replace(' ', '-')}/view/report/{report.name}",
	}
	out.update(extra)
	if auto:
		out["auto_filled_filters"] = auto
	if tree:
		out["note"] = "Hierarchical report (indented rows include group totals) - do not add rows together; read group/total rows instead."
	if len(rows) > len(out_rows):
		out["more"] = f"{len(rows) - len(out_rows)} more rows not shown; column_totals cover all {len(rows)} rows."
	return out


def _run_report_builder(report, extra_filters):
	cfg = json.loads(report.json or "{}")
	doctype = report.ref_doctype
	from fibersoft_ai.ai.tools.query import prepare

	meta = prepare(doctype)
	saved = cfg.get("filters") or []
	cleaned = []
	for f in saved:
		if isinstance(f, (list, tuple)) and len(f) >= 4:
			cleaned.append(list(f[1:4]) if f[0] == doctype else list(f[:4]))
		elif isinstance(f, (list, tuple)) and len(f) == 3:
			cleaned.append(list(f))
	flt_list = normalize_filters(meta, cleaned) if cleaned else []
	flt_list += normalize_filters(meta, extra_filters or {})
	fields = []
	for c in cfg.get("columns") or cfg.get("fields") or []:
		if isinstance(c, (list, tuple)) and len(c) >= 2 and c[1] == doctype:
			fields.append(c[0])
		elif isinstance(c, str) and "." not in c:
			fields.append(c.strip("`"))
	from fibersoft_ai.ai.tools.query import validate_fields

	fields = validate_fields(meta, fields or None)
	ob = cfg.get("order_by") or {}
	order = None
	if isinstance(ob, dict) and ob.get("order_by"):
		try:
			order = validate_order_by(meta, f"{ob['order_by'].split('.')[-1].strip('`')} {ob.get('sort_order', 'desc')}")
		except ToolError:
			order = None
	rows = run_get_list(doctype, filters=flt_list, fields=fields, order_by=order, limit_page_length=5000)
	cols = []
	for f in fields:
		df = meta.get_field(f)
		cols.append({"fieldname": f, "label": df.label if df else f, "fieldtype": df.fieldtype if df else "Data",
					 "options": df.options if df and df.fieldtype == "Link" else None})
	extra = {}
	if len(rows) == 5000:
		extra["note_limit"] = "Report Builder results capped at 5000 rows; add filters for complete totals."
	return cols, [dict(r) for r in rows], extra
