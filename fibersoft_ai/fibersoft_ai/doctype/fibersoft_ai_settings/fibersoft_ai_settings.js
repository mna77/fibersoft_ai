// Copyright (c) 2026, Fibersoft and contributors
// For license information, please see license.txt

frappe.ui.form.on("Fibersoft AI Settings", {
	refresh(frm) {
		frm.trigger("render_usage");
		frm.add_custom_button(__("Token Limits"), () => frappe.set_route("List", "Fibersoft AI Token Limit"), __("View"));
		frm.add_custom_button(__("Usage Report"), () => frappe.set_route("query-report", "Fibersoft AI Usage"), __("View"));
		frm.add_custom_button(__("Action Log"), () => frappe.set_route("List", "Fibersoft AI Action"), __("View"));
		frm.add_custom_button(__("Open Assistant"), () => {
			if (window.fibersoft_ai_widget) window.fibersoft_ai_widget.open();
			else frappe.msgprint(__("Enable the assistant, save, and reload the page."));
		});
	},

	test_connection_btn(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the settings first."));
			return;
		}
		frappe.call({
			method: "fibersoft_ai.api.test_connection",
			freeze: true,
			freeze_message: __("Contacting Claude API..."),
			callback(r) {
				const m = r.message || {};
				frappe.msgprint({
					title: m.ok ? __("Connection successful") : __("Connection failed"),
					indicator: m.ok ? "green" : "red",
					message: frappe.utils.escape_html(m.message || ""),
				});
			},
		});
	},

	render_usage(frm) {
		const wrapper = frm.get_field("usage_html").$wrapper;
		wrapper.html(`<div class="text-muted small">${__("Loading usage...")}</div>`);
		frappe.call({
			method: "fibersoft_ai.api.get_usage_stats",
			callback(r) {
				const s = r.message || {};
				const card = (label, value, sub) => `
					<div style="flex:1;min-width:140px;border:1px solid var(--border-color);border-radius:10px;padding:10px 12px;background:var(--fg-color)">
						<div class="text-muted small">${label}</div>
						<div style="font-size:20px;font-weight:600">${value}</div>
						<div class="text-muted small">${sub || ""}</div>
					</div>`;
				const top = (s.top_users || [])
					.map(
						(u) =>
							`<tr><td>${frappe.utils.escape_html(u.user)}</td><td class="text-right">${u.questions}</td><td class="text-right">${format_number(u.tokens || 0, null, 0)}</td><td class="text-right">$${(u.cost || 0).toFixed(4)}</td></tr>`
					)
					.join("");
				wrapper.html(`
					<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">
						${card(__("Questions this month"), s.questions || 0, __("{0} today", [s.questions_today || 0]))}
						${card(__("Estimated cost this month"), "$" + (s.cost || 0).toFixed(4), s.budget ? __("Budget ${0}", [s.budget]) : __("No budget cap"))}
						${card(__("Counted tokens this month"), format_number(s.tokens || 0, null, 0), __("see Token Limits to allocate"))}
						${card(__("Active users"), s.users || 0, __("this month"))}
						${card(__("Answer rating"), s.good + s.bad ? Math.round((100 * s.good) / (s.good + s.bad)) + "%" : "-", __("{0} rated", [s.good + s.bad]))}
						${card(__("Actions executed"), s.actions_executed || 0, __("{0} pending", [s.actions_pending || 0]))}
					</div>
					${top ? `<table class="table table-sm table-bordered" style="max-width:640px"><thead><tr><th>${__("User")}</th><th class="text-right">${__("Questions")}</th><th class="text-right">${__("Tokens")}</th><th class="text-right">${__("Cost")}</th></tr></thead><tbody>${top}</tbody></table>` : ""}
				`);
			},
		});
	},
});
