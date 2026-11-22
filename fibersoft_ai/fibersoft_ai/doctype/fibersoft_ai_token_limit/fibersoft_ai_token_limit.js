// Copyright (c) 2026, Fibersoft and contributors
// For license information, please see license.txt

frappe.ui.form.on("Fibersoft AI Token Limit", {
	refresh(frm) {
		frm.trigger("render_usage");
		frm.add_custom_button(__("Usage Report"), () => frappe.set_route("query-report", "Fibersoft AI Usage"));
		if (!frm.is_new()) {
			frm.add_custom_button(__("Copy limits to users..."), () => frm.trigger("copy_to_users"));
		}
		frm.set_intro(
			__("0 means unlimited. A User rule wins over Role rules; among Role rules the highest Priority wins; the Everyone rule covers everyone else."),
			"blue"
		);
	},
	limit_for(frm) {
		frm.trigger("render_usage");
	},
	user(frm) {
		frm.trigger("render_usage");
	},

	render_usage(frm) {
		const $w = frm.get_field("usage_html").$wrapper;
		if (frm.is_new() && frm.doc.limit_for !== "User") {
			$w.html(`<div class="text-muted small">${__("Save to see which users this rule covers.")}</div>`);
			return;
		}
		if (frm.doc.limit_for === "User" && !frm.doc.user) {
			$w.html("");
			return;
		}
		$w.html(`<div class="text-muted small">${__("Loading usage...")}</div>`);
		frappe.call({
			method: "fibersoft_ai.api.get_user_usage",
			args: frm.doc.limit_for === "User" ? { user: frm.doc.user } : { rule: frm.doc.name },
			callback(r) {
				const data = r.message || {};
				const users = data.users || [];
				if (!users.length) {
					$w.html(`<div class="text-muted small">${__("No users are currently governed by this rule (a more specific User/Role rule may apply).")}</div>`);
					return;
				}
				const fmt = (n) => format_number(n || 0, null, 0);
				const bar = (e) => {
					if (!e.limit) return `<div class="small"><b>${fmt(e.used)}</b> <span class="text-muted">/ ${__("unlimited")}</span></div>`;
					const pct = Math.min(e.percent || 0, 100);
					const color = pct >= 90 ? "#dc2626" : pct >= 70 ? "#d97706" : "#111111";
					return `<div class="small"><b>${fmt(e.used)}</b> <span class="text-muted">/ ${fmt(e.limit)} (${e.percent}%)</span></div>
						<div style="height:6px;background:var(--gray-200);border-radius:4px;overflow:hidden;margin-top:3px">
							<div style="height:100%;width:${pct}%;background:${color}"></div></div>`;
				};
				const rows = users
					.map((u) => {
						const p = u.periods;
						const governed = u.rule === frm.doc.name || frm.doc.limit_for === "User";
						return `<tr>
							<td><b>${frappe.utils.escape_html(u.full_name || u.user)}</b><div class="text-muted small">${frappe.utils.escape_html(u.user)}</div>
								${governed ? "" : `<div class="small" style="color:#d97706">${__("Governed by")}: ${frappe.utils.escape_html(u.source)}</div>`}</td>
							<td style="min-width:150px">${bar(p.daily)}</td>
							<td style="min-width:150px">${bar(p.weekly)}</td>
							<td style="min-width:150px">${bar(p.monthly)}</td>
							<td class="text-right small">${p.monthly.questions}<div class="text-muted">$${(p.monthly.cost || 0).toFixed(4)}</div></td>
						</tr>`;
					})
					.join("");
				$w.html(`
					<table class="table table-bordered" style="margin:0">
						<thead><tr><th>${__("User")}</th><th>${__("Today")}</th><th>${__("This Week")}</th><th>${__("This Month")}</th><th class="text-right">${__("Questions / Cost (month)")}</th></tr></thead>
						<tbody>${rows}</tbody>
					</table>
					${data.total_users > users.length ? `<div class="text-muted small" style="margin-top:6px">${__("Showing top {0} of {1} users", [users.length, data.total_users])}</div>` : ""}`);
			},
		});
	},

	copy_to_users(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Create User rules with these limits"),
			fields: [
				{ fieldname: "user_list", fieldtype: "MultiSelectList", label: __("Users"), reqd: 1,
				  get_data: (txt) => frappe.db.get_link_options("User", txt, { user_type: "System User", enabled: 1 }) }],
			primary_action_label: __("Create"),
			primary_action(v) {
				frappe.call({
					method: "fibersoft_ai.api.copy_limit_to_users",
					args: { rule: frm.doc.name, users: v.user_list },
					freeze: true,
					callback(r) {
						d.hide();
						frappe.msgprint(__("Created {0} rule(s), skipped {1} user(s) that already have a rule.", [r.message.created, r.message.skipped]));
					},
				});
			},
		});
		d.show();
	},
});
