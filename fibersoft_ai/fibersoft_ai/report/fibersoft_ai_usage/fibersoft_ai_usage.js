// Copyright (c) 2026, Fibersoft and contributors

frappe.query_reports["Fibersoft AI Usage"] = {
	filters: [
		{ fieldname: "period", label: __("Period"), fieldtype: "Select", options: "Today\nThis Week\nThis Month\nCustom", default: "This Month", reqd: 1 },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", depends_on: "eval:doc.period=='Custom'" },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", depends_on: "eval:doc.period=='Custom'" },
		{ fieldname: "user", label: __("User"), fieldtype: "Link", options: "User" },
		{ fieldname: "include_idle", label: __("Include users with a rule but no usage"), fieldtype: "Check" },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "used_pct" && data && data.used_pct !== null && data.used_pct !== undefined) {
			const color = data.used_pct >= 90 ? "#dc2626" : data.used_pct >= 70 ? "#d97706" : "#16a34a";
			value = `<span style="color:${color};font-weight:600">${value}</span>`;
		}
		return value;
	},
	onload(report) {
		report.page.add_inner_button(__("Token Limits"), () => frappe.set_route("List", "Fibersoft AI Token Limit"));
	},
};
