// Fibersoft AI - launcher icon + chat panel on every desk page.
import { FibersoftAI } from "./fibersoft_ai/widget";

frappe.provide("fibersoft_ai");
fibersoft_ai.FibersoftAI = FibersoftAI;

function start(attempt = 0) {
	const ready = window.frappe && frappe.boot && frappe.session && frappe.session.user && document.body && window.$ && frappe.realtime;
	if (!ready) {
		if (attempt < 150) setTimeout(() => start(attempt + 1), 200);
		return;
	}
	if (window.fibersoft_ai_widget || frappe.session.user === "Guest") return;
	const cfg = frappe.boot.fibersoft_ai;
	if (!cfg || !cfg.allowed) return;
	try {
		new FibersoftAI(cfg);
	} catch (e) {
		console.error("Fibersoft AI failed to start", e);
	}
}

$(document).ready(() => start());
