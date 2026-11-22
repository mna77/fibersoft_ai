// Captures what the user is looking at, so the assistant can answer about "this" screen.
// Only data already visible to the user in the browser is captured; the server re-checks
// permissions for anything it reads itself.

const NO_VALUE = new Set(["Section Break", "Column Break", "Tab Break", "HTML", "Button", "Image", "Fold", "Heading"]);
const SENSITIVE = /password|secret|api_key|token|private_key/i;
const MAX_ROWS = 60;
const MAX_TEXT = 6000;

const strip = (html) => {
	if (html === null || html === undefined) return html;
	const s = String(html);
	if (!/[<&]/.test(s)) return s;
	const div = document.createElement("div");
	div.innerHTML = s;
	return (div.textContent || "").trim();
};

const trimValue = (v) => {
	if (typeof v === "string") {
		const t = /<[a-z][\s\S]*>/i.test(v) ? strip(v) : v;
		return t.length > 800 ? t.slice(0, 800) + "…" : t;
	}
	return v;
};

const empty = (v) => v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length);

function cleanRow(meta, row) {
	const out = {};
	(meta?.fields || []).forEach((df) => {
		if (NO_VALUE.has(df.fieldtype) || df.fieldtype === "Password" || SENSITIVE.test(df.fieldname)) return;
		const v = row[df.fieldname];
		if (!empty(v)) out[df.fieldname] = trimValue(v);
	});
	if (row.name && !String(row.name).startsWith("new-")) out.name = row.name;
	return out;
}

function formContext(frm) {
	const doc = frm.doc || {};
	const meta = frm.meta || frappe.get_meta(frm.doctype);
	const data = {};
	(meta.fields || []).forEach((df) => {
		if (NO_VALUE.has(df.fieldtype) || df.fieldtype === "Password" || SENSITIVE.test(df.fieldname)) return;
		const v = doc[df.fieldname];
		if (empty(v)) return;
		if (df.fieldtype === "Table" || df.fieldtype === "Table MultiSelect") {
			const cmeta = frappe.get_meta(df.options);
			data[df.fieldname] = v.slice(0, MAX_ROWS).map((r) => cleanRow(cmeta, r));
			if (v.length > MAX_ROWS) data[df.fieldname + "__note"] = `first ${MAX_ROWS} of ${v.length} rows`;
		} else {
			data[df.fieldname] = trimValue(v);
		}
	});
	const messages = [];
	try {
		frm.layout?.wrapper?.find(".form-message:visible, .form-dashboard-section .alert:visible").each(function () {
			const t = (this.innerText || "").trim();
			if (t) messages.push(t.slice(0, 300));
		});
	} catch (e) {
		/* ignore */
	}
	const ctx = {
		view: "form",
		doctype: frm.doctype,
		docname: frm.is_new() ? null : frm.docname,
		is_new: !!frm.is_new(),
		is_dirty: !!frm.is_dirty(),
		docstatus: doc.docstatus,
		status_indicator: (frm.page?.indicator?.text?.() || "").trim() || undefined,
		doc: data,
	};
	if (meta.is_submittable) ctx.docstatus_meaning = { 0: "Draft", 1: "Submitted", 2: "Cancelled" }[doc.docstatus];
	if (messages.length) ctx.form_messages = messages;
	return ctx;
}

function listContext(list, route) {
	const meta = frappe.get_meta(list.doctype);
	let filters = [];
	try {
		filters = list.get_filters_for_args ? list.get_filters_for_args() : list.filters || [];
	} catch (e) {
		filters = [];
	}
	const rows = (list.data || []).slice(0, 40).map((r) => {
		const out = {};
		Object.keys(r).forEach((k) => {
			if (k.startsWith("_") || SENSITIVE.test(k) || empty(r[k])) return;
			out[k] = trimValue(r[k]);
		});
		return out;
	});
	return {
		view: (route[2] || "List").toLowerCase() === "report" ? "report_view" : "list",
		doctype: list.doctype,
		filters,
		sort: list.sort_by ? `${list.sort_by} ${list.sort_order || ""}`.trim() : undefined,
		visible_rows: rows,
		visible_row_count: (list.data || []).length,
		total_count_shown: (list.$result?.find?.(".list-count")?.text?.() || "").trim() || undefined,
		is_submittable: !!meta?.is_submittable,
	};
}

function reportContext(qr) {
	const cols = (qr.columns || [])
		.filter((c) => c && (c.fieldname || c.id))
		.map((c) => ({ fieldname: c.fieldname || c.id, label: strip(c.label || c.name || ""), fieldtype: c.fieldtype }));
	const names = cols.map((c) => c.fieldname);
	const data = Array.isArray(qr.data) ? qr.data : [];
	const rows = data.slice(0, MAX_ROWS).map((r) => {
		const out = {};
		names.forEach((n) => {
			if (!empty(r[n])) out[n] = trimValue(r[n]);
		});
		if (r.indent) out.indent = r.indent;
		return out;
	});
	let filters = {};
	try {
		filters = qr.get_filter_values ? qr.get_filter_values(false) || {} : {};
	} catch (e) {
		filters = {};
	}
	const summary = (qr.raw_data?.report_summary || []).map((s) => ({ label: strip(s.label), value: s.value }));
	return {
		view: "query_report",
		report_name: qr.report_name,
		ref_doctype: qr.report_doc?.ref_doctype,
		filters,
		columns: cols,
		rows,
		total_rows_on_screen: data.length,
		rows_note: data.length > MAX_ROWS ? `only first ${MAX_ROWS} rows included; rerun the report for exact totals` : undefined,
		report_summary: summary.length ? summary : undefined,
	};
}

function pageText(selector) {
	const el = document.querySelector(selector);
	if (!el) return undefined;
	const t = (el.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
	return t.length > MAX_TEXT ? t.slice(0, MAX_TEXT) + "…" : t;
}

function workspaceContext(route) {
	const cards = [];
	document.querySelectorAll(".page-container:not([style*='display: none']) .widget.number-widget-box").forEach((w) => {
		const title = w.querySelector(".widget-title")?.innerText?.trim();
		const value = w.querySelector(".number")?.innerText?.trim();
		if (title) cards.push({ title, value });
	});
	const charts = [];
	document.querySelectorAll(".page-container:not([style*='display: none']) .widget.dashboard-widget-box .widget-title").forEach((t) => {
		if (t.innerText.trim()) charts.push(t.innerText.trim());
	});
	return {
		view: route[0] === "dashboard-view" ? "dashboard" : "workspace",
		name: route[1] || frappe.router?.current_route?.[1],
		number_cards: cards.length ? cards : undefined,
		charts: charts.length ? charts : undefined,
		visible_text: pageText(".page-container:not([style*='display: none']) .layout-main-section"),
	};
}

export function captureContext({ lastError, selection } = {}) {
	const route = (frappe.get_route && frappe.get_route()) || [];
	let ctx = { route, page_title: document.title };
	try {
		const view = route[0];
		if (view === "Form" && window.cur_frm && cur_frm.doctype === route[1]) {
			Object.assign(ctx, formContext(cur_frm));
		} else if (view === "List" && window.cur_list && cur_list.doctype === route[1]) {
			Object.assign(ctx, listContext(cur_list, route));
		} else if (view === "query-report" && frappe.query_report && frappe.query_report.report_name) {
			Object.assign(ctx, reportContext(frappe.query_report));
		} else if (view === "Workspaces" || view === "dashboard-view" || !view) {
			Object.assign(ctx, workspaceContext(route));
		} else {
			ctx.view = "page";
			ctx.visible_text = pageText(".page-container:not([style*='display: none']) .layout-main-section");
		}
	} catch (e) {
		console.warn("Fibersoft AI: could not capture screen context", e);
	}
	if (lastError && Date.now() - lastError.at < 15 * 60 * 1000) ctx.last_error = lastError.text;
	if (selection) ctx.selected_text = selection;
	Object.keys(ctx).forEach((k) => ctx[k] === undefined && delete ctx[k]);
	return ctx;
}

export function describeContext(ctx) {
	if (!ctx) return __("No screen shared");
	switch (ctx.view) {
		case "form":
			return ctx.is_new ? __("New {0}", [__(ctx.doctype)]) : `${__(ctx.doctype)} ${ctx.docname}`;
		case "list":
			return __("{0} list", [__(ctx.doctype)]);
		case "report_view":
			return __("{0} report view", [__(ctx.doctype)]);
		case "query_report":
			return __("Report: {0}", [__(ctx.report_name)]);
		case "dashboard":
			return __("Dashboard: {0}", [ctx.name || ""]);
		case "workspace":
			return __("Workspace: {0}", [ctx.name || __("Home")]);
		default:
			return ctx.page_title || __("This page");
	}
}

export function stripHtml(s) {
	return strip(s);
}
