import { renderMarkdown, escapeHtml as esc } from "./markdown";
import { captureContext, describeContext, stripHtml } from "./context";

const API = "fibersoft_ai.api.";
const EVENT = "fibersoft_ai";
const POLL_MS = 1500;

const ICONS = {
	history: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></svg>',
	plus: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
	expand: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
	shrink: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>',
	close: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
	send: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12.6 2-12.6 2z"/></svg>',
	stop: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
	mic: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
	copy: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
	up: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v11H3V10zM7 10l4-8a3 3 0 0 1 3 3v4h6a2 2 0 0 1 2 2.3l-1.4 8A2 2 0 0 1 18.6 21H7"/></svg>',
	down: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V3h4v11zM17 14l-4 8a3 3 0 0 1-3-3v-4H4a2 2 0 0 1-2-2.3l1.4-8A2 2 0 0 1 5.4 3H17"/></svg>',
	eye: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>',
	trash: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>',
	retry: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>',
};

const ACTION_LABELS = {
	Create: __("Create"),
	Update: __("Update"),
	Submit: __("Submit"),
	Cancel: __("Cancel"),
	Workflow: __("Workflow"),
	Comment: __("Comment"),
	Assign: __("Assign"),
};

function storage(key, value) {
	try {
		if (value === undefined) return localStorage.getItem(key);
		if (value === null) localStorage.removeItem(key);
		else localStorage.setItem(key, value);
	} catch (e) {
		return null;
	}
}

function call(method, args = {}, opts = {}) {
	return new Promise((resolve, reject) => {
		frappe.call({
			method: API + method,
			args,
			type: opts.type || "POST",
			freeze: false,
			callback: (r) => resolve(r.message),
			error: (r) => reject(r),
		});
	});
}

export class FibersoftAI {
	constructor(cfg) {
		this.cfg = cfg;
		this.user = frappe.session.user;
		this.conversation = storage(`fai:conv:${this.user}`) || null;
		this.loadedConversation = null;
		this.busy = false;
		this.turn = null;
		this.includeContext = storage("fai:ctx") !== "0";
		this.lastError = null;
		this.selection = null;
		this.maximized = storage("fai:max") === "1";
		this.make();
		this.bind();
		this.watchErrors();
		this.watchSelection();
		this.updateContextLabel();
		window.fibersoft_ai_widget = this;
	}

	// ------------------------------------------------------------------ DOM
	make() {
		const c = this.cfg;
		const name = esc(c.assistant_name);
		this.$root = $(`
<div class="fai-root fai-pos-${c.position === "left" ? "left" : "right"}" style="--fai-accent:${esc(c.theme_color)}">
	<button class="fai-launcher" type="button" title="${name} (Ctrl+Shift+K)" aria-label="${name}">
		<img src="${esc(c.icon)}" alt="" draggable="false">
		<span class="fai-launcher-badge" hidden></span>
	</button>
	<section class="fai-panel${this.maximized ? " fai-max" : ""}" role="dialog" aria-label="${name}" hidden>
		<header class="fai-header">
			<img class="fai-logo" src="${esc(c.icon)}" alt="">
			<div class="fai-titles">
				<div class="fai-title">${name}</div>
				<div class="fai-subtitle"><span class="fai-status-dot"></span><span class="fai-ctx-label"></span></div>
			</div>
			<div class="fai-head-actions">
				<button class="fai-icon-btn" data-act="history" title="${__("Chat history")}">${ICONS.history}</button>
				<button class="fai-icon-btn" data-act="new" title="${__("New chat")}">${ICONS.plus}</button>
				<button class="fai-icon-btn fai-hide-mobile" data-act="max" title="${__("Expand")}">${this.maximized ? ICONS.shrink : ICONS.expand}</button>
				<button class="fai-icon-btn" data-act="close" title="${__("Close")}">${ICONS.close}</button>
			</div>
		</header>
		<div class="fai-history" hidden>
			<div class="fai-history-head">
				<input class="fai-history-search" type="search" placeholder="${__("Search chats")}">
			</div>
			<div class="fai-history-list"></div>
		</div>
		<div class="fai-body"><div class="fai-messages"></div></div>
		<footer class="fai-footer">
			<div class="fai-chips">
				<button class="fai-chip fai-chip-ctx" type="button" title="${__("Share this screen with the assistant")}">${ICONS.eye}<span></span></button>
				<button class="fai-chip fai-chip-sel" type="button" hidden title="${__("Remove selected text")}"><span></span> ✕</button>
			</div>
			<div class="fai-composer">
				<textarea rows="1" placeholder="${__("Ask about this screen, a record, a report...")}" maxlength="8000"></textarea>
				${c.voice && (window.SpeechRecognition || window.webkitSpeechRecognition) ? `<button class="fai-icon-btn fai-mic" type="button" title="${__("Speak")}">${ICONS.mic}</button>` : ""}
				<button class="fai-send" type="button" title="${__("Send")}">${ICONS.send}</button>
			</div>
			<div class="fai-meter" hidden><div class="fai-meter-text"></div><div class="fai-meter-bar"><i></i></div></div>
			<div class="fai-hint">${__("AI can make mistakes. Check important figures before acting on them.")}</div>
		</footer>
	</section>
</div>`);
		$("body").append(this.$root);
		this.$panel = this.$root.find(".fai-panel");
		this.$messages = this.$root.find(".fai-messages");
		this.$body = this.$root.find(".fai-body");
		this.$input = this.$root.find("textarea");
		this.$send = this.$root.find(".fai-send");
		this.$history = this.$root.find(".fai-history");
		this.renderCtxChip();
	}

	bind() {
		this.$root.find(".fai-launcher").on("click", () => this.toggle());
		this.$root.find(".fai-head-actions").on("click", "button", (e) => {
			const act = $(e.currentTarget).data("act");
			if (act === "close") this.close();
			if (act === "new") this.newChat();
			if (act === "max") this.toggleMax();
			if (act === "history") this.toggleHistory();
		});
		this.$send.on("click", () => (this.busy ? this.stop() : this.send()));
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
				e.preventDefault();
				if (!this.busy) this.send();
			}
			if (e.key === "Escape") this.close();
		});
		this.$input.on("input", () => this.autosize());
		this.$body.on("scroll", () => {
			const el = this.$body[0];
			// stop following the stream if the user scrolls up to read
			this.autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
		});
		this.$root.find(".fai-chip-ctx").on("click", () => {
			this.includeContext = !this.includeContext;
			storage("fai:ctx", this.includeContext ? "1" : "0");
			this.renderCtxChip();
		});
		this.$root.find(".fai-chip-sel").on("click", () => {
			this.selection = null;
			this.renderSelChip();
		});
		this.$root.find(".fai-mic").on("click", () => this.toggleVoice());
		this.$messages.on("click", "a[data-fai-internal]", (e) => {
			e.preventDefault();
			const href = $(e.currentTarget).attr("href");
			if (href.startsWith("/app/")) frappe.set_route(decodeURIComponent(href.slice(5)));
			else window.location.href = href;
			if (window.innerWidth < 640) this.close();
		});
		this.$messages.on("click", "[data-suggest]", (e) => {
			this.$input.val($(e.currentTarget).attr("data-suggest"));
			this.send();
		});
		this.$history.on("input", ".fai-history-search", frappe.utils.debounce(() => this.loadHistory(), 300));
		this.$history.on("click", ".fai-history-item", (e) => {
			if ($(e.target).closest(".fai-history-del").length) return;
			this.openConversation($(e.currentTarget).data("name"));
		});
		this.$history.on("click", ".fai-history-del", (e) => {
			e.stopPropagation();
			const name = $(e.currentTarget).closest(".fai-history-item").data("name");
			frappe.confirm(__("Delete this chat?"), () =>
				call("delete_conversation", { conversation: name }).then(() => {
					if (name === this.conversation) this.newChat();
					this.loadHistory();
				})
			);
		});
		$(document).on("keydown.fai", (e) => {
			if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "K" || e.key === "k")) {
				e.preventDefault();
				this.toggle();
			}
		});
		frappe.realtime.on(EVENT, (data) => this.onRealtime(data));
		if (frappe.router && frappe.router.on) frappe.router.on("change", () => setTimeout(() => this.updateContextLabel(), 400));
		$(document).on("page-change.fai form-refresh.fai", () => setTimeout(() => this.updateContextLabel(), 400));
	}

	watchErrors() {
		const self = this;
		const orig = frappe.msgprint;
		if (!orig || orig.__fai) return;
		const wrapped = function (msg, title) {
			try {
				let text = msg,
					t = title,
					indicator = "";
				if (msg && typeof msg === "object") {
					text = msg.message;
					t = msg.title || title;
					indicator = msg.indicator || "";
				}
				const plain = stripHtml(Array.isArray(text) ? text.join("\n") : text);
				if (plain && (indicator === "red" || /error|missing|not allowed|cannot|invalid|mandatory|required/i.test(`${t} ${plain}`))) {
					self.lastError = { text: (t ? stripHtml(t) + ": " : "") + plain.slice(0, 2000), at: Date.now() };
				}
			} catch (e) {
				/* never break msgprint */
			}
			return orig.apply(this, arguments);
		};
		wrapped.__fai = true;
		frappe.msgprint = wrapped;
	}

	watchSelection() {
		document.addEventListener("selectionchange", () => {
			const sel = window.getSelection();
			if (!sel || sel.isCollapsed) return;
			const node = sel.anchorNode;
			if (node && this.$root[0].contains(node)) return;
			const text = sel.toString().trim();
			if (text.length >= 3) this.pendingSelection = { text: text.slice(0, 3000), at: Date.now() };
		});
	}

	// ------------------------------------------------------------------ panel state
	toggle() {
		this.$panel.prop("hidden") ? this.open() : this.close();
	}

	open() {
		if (this.pendingSelection && Date.now() - this.pendingSelection.at < 60000) {
			this.selection = this.pendingSelection.text;
			this.pendingSelection = null;
		}
		this.$panel.prop("hidden", false);
		this.$root.addClass("fai-open");
		this.$root.find(".fai-launcher-badge").prop("hidden", true);
		this.updateContextLabel();
		this.renderSelChip();
		this.refreshUsage();
		if (this.conversation && this.loadedConversation !== this.conversation) this.openConversation(this.conversation);
		else if (!this.$messages.children().length) this.renderWelcome();
		setTimeout(() => this.$input.trigger("focus"), 50);
	}

	close() {
		this.$panel.prop("hidden", true);
		this.$root.removeClass("fai-open");
		this.stopVoice();
	}

	toggleMax() {
		this.maximized = !this.maximized;
		storage("fai:max", this.maximized ? "1" : "0");
		this.$panel.toggleClass("fai-max", this.maximized);
		this.$root.find('[data-act="max"]').html(this.maximized ? ICONS.shrink : ICONS.expand);
	}

	toggleHistory() {
		const show = this.$history.prop("hidden");
		this.$history.prop("hidden", !show);
		if (show) this.loadHistory();
	}

	newChat() {
		if (this.busy) return;
		this.conversation = null;
		this.loadedConversation = null;
		storage(`fai:conv:${this.user}`, null);
		this.$history.prop("hidden", true);
		this.$messages.empty();
		this.renderWelcome();
		this.$input.trigger("focus");
	}

	autosize() {
		const el = this.$input[0];
		el.style.height = "auto";
		el.style.height = Math.min(el.scrollHeight, 160) + "px";
	}

	scrollBottom(force) {
		const el = this.$body[0];
		if (force) this.autoScroll = true;
		if (force || this.autoScroll !== false) el.scrollTop = el.scrollHeight;
	}

	// ------------------------------------------------------------------ context
	currentContext() {
		return captureContext({ lastError: this.lastError, selection: this.selection });
	}

	updateContextLabel() {
		let label;
		try {
			label = describeContext(captureContext({}));
		} catch (e) {
			label = "";
		}
		this.contextLabel = label;
		this.$root.find(".fai-ctx-label").text(this.includeContext ? __("Viewing: {0}", [label]) : __("Screen not shared"));
		this.renderCtxChip();
		if (!this.$panel.prop("hidden") && this.$messages.find(".fai-welcome").length) this.renderWelcome();
	}

	renderCtxChip() {
		const chip = this.$root.find(".fai-chip-ctx");
		chip.toggleClass("fai-chip-off", !this.includeContext);
		chip.find("span").text(this.includeContext ? this.contextLabel || __("This screen") : __("Screen not shared"));
	}

	renderSelChip() {
		const chip = this.$root.find(".fai-chip-sel");
		chip.prop("hidden", !this.selection);
		if (this.selection) chip.find("span").text(__("Selected: {0}", [this.selection.slice(0, 28) + (this.selection.length > 28 ? "…" : "")]));
	}

	suggestions() {
		const ctx = captureContext({});
		const dt = ctx.doctype ? __(ctx.doctype) : "";
		const s = [];
		if (ctx.view === "form" && !ctx.is_new) {
			s.push(__("Summarise this {0}", [dt]));
			if (ctx.docstatus === 0) s.push(__("Is anything missing before I can submit it?"));
			s.push(__("Who changed this and when?"));
			if (["Customer", "Supplier"].includes(ctx.doctype)) s.push(__("What is the outstanding balance and ageing?"));
			if (["Sales Invoice", "Purchase Invoice"].includes(ctx.doctype)) s.push(__("Has this been paid? Show payments."));
			if (ctx.doctype === "Item") s.push(__("Show current stock of this item by warehouse"));
		} else if (ctx.view === "form") {
			s.push(__("Help me fill this {0}", [dt]));
		} else if (ctx.view === "list" || ctx.view === "report_view") {
			s.push(__("Summarise these {0} records", [dt]));
			const meta = frappe.get_meta(ctx.doctype);
			const money = (meta?.fields || []).find((f) => f.fieldtype === "Currency" && f.in_list_view) || (meta?.fields || []).find((f) => f.fieldtype === "Currency");
			if ((meta?.fields || []).some((f) => f.fieldname === "status")) s.push(__("Totals by status"));
			if (money) s.push(__("Top 10 by {0} this month", [__(money.label)]));
			else s.push(__("How many were created this month?"));
		} else if (ctx.view === "query_report") {
			s.push(__("Summarise this report"));
			s.push(__("Show the top 10 rows and a chart"));
			s.push(__("Anything unusual I should look at?"));
		} else if (ctx.view === "workspace" || ctx.view === "dashboard") {
			s.push(__("Explain the numbers on this page"));
			s.push(__("What needs my attention today?"));
		}
		if (this.lastError && Date.now() - this.lastError.at < 15 * 60 * 1000) s.unshift(__("Explain the last error and how to fix it"));
		if (s.length < 3) {
			s.push(__("Sales this month vs last month"));
			s.push(__("Which customers have overdue payments?"));
			s.push(__("What can you help me with?"));
		}
		return s.slice(0, 4);
	}

	renderWelcome() {
		const c = this.cfg;
		let warn = "";
		if (!c.configured) {
			warn = c.is_manager
				? `<div class="fai-warn">${__("Add your Claude API key in {0} to start.", [`<a href="/app/fibersoft-ai-settings" data-fai-internal="1">${__("Fibersoft AI Settings")}</a>`])}</div>`
				: `<div class="fai-warn">${__("The assistant is not configured yet. Please contact your System Manager.")}</div>`;
		}
		this.$messages.html(`
			<div class="fai-welcome">
				<img src="${esc(c.icon)}" alt="">
				<div class="fai-welcome-title">${esc(c.assistant_name)}</div>
				<div class="fai-welcome-text">${esc(c.welcome_message || "")}</div>
				${warn}
				<div class="fai-suggest-grid">
					${this.suggestions().map((s) => `<button class="fai-suggest" data-suggest="${esc(s)}">${esc(s)}</button>`).join("")}
				</div>
				${c.actions ? `<div class="fai-welcome-foot">${__("I can also prepare changes (create, update, submit, approve...) - you always confirm first.")}</div>` : ""}
			</div>`);
	}

	// ------------------------------------------------------------------ history
	loadHistory() {
		const search = this.$history.find(".fai-history-search").val();
		const $list = this.$history.find(".fai-history-list").html(`<div class="fai-muted">${__("Loading...")}</div>`);
		call("list_conversations", { search }, { type: "GET" }).then((rows) => {
			if (!rows || !rows.length) {
				$list.html(`<div class="fai-muted">${__("No chats yet")}</div>`);
				return;
			}
			$list.html(
				rows
					.map(
						(r) => `<div class="fai-history-item${r.name === this.conversation ? " active" : ""}" data-name="${esc(r.name)}">
					<div class="fai-history-title">${esc(r.title || __("Untitled"))}</div>
					<div class="fai-history-meta">${esc(frappe.datetime.prettyDate ? frappe.datetime.prettyDate(r.last_message_on) : r.last_message_on || "")}</div>
					<button class="fai-icon-btn fai-history-del" title="${__("Delete")}">${ICONS.trash}</button>
				</div>`
					)
					.join("")
			);
		});
	}

	openConversation(name) {
		this.$history.prop("hidden", true);
		this.$messages.html(`<div class="fai-muted fai-center">${__("Loading chat...")}</div>`);
		call("get_conversation", { conversation: name }, { type: "GET" })
			.then((data) => {
				this.conversation = name;
				this.loadedConversation = name;
				storage(`fai:conv:${this.user}`, name);
				this.$messages.empty();
				(data.messages || []).forEach((m) => {
					if (m.role === "user") this.addUser(m.content);
					else if (m.role === "assistant") {
						const $m = this.addAssistant();
						this.finishAssistant($m, {
							text: m.content,
							error: m.status === "Error" ? m.error : null,
							actions: m.actions || [],
							message: m.name,
							feedback: m.feedback,
							steps: [],
						});
					} else if (m.role === "event") this.addEvent(m.content);
				});
				if (!this.$messages.children().length) this.renderWelcome();
				this.scrollBottom(true);
			})
			.catch(() => {
				storage(`fai:conv:${this.user}`, null);
				this.conversation = null;
				this.renderWelcome();
			});
	}

	// ------------------------------------------------------------------ messages
	addUser(text) {
		this.$messages.find(".fai-welcome").remove();
		const $m = $(`<div class="fai-msg fai-user"><div class="fai-bubble"></div></div>`);
		$m.find(".fai-bubble").text(text);
		this.$messages.append($m);
		this.scrollBottom(true);
		return $m;
	}

	addEvent(text) {
		this.$messages.append(`<div class="fai-event">${esc(text)}</div>`);
	}

	addAssistant() {
		const $m = $(`
			<div class="fai-msg fai-assistant">
				<img class="fai-avatar" src="${esc(this.cfg.icon)}" alt="">
				<div class="fai-content">
					<div class="fai-steps" hidden></div>
					<div class="fai-text"><span class="fai-typing"><i></i><i></i><i></i></span></div>
					<div class="fai-actions"></div>
					<div class="fai-msg-foot" hidden></div>
				</div>
			</div>`);
		this.$messages.append($m);
		this.scrollBottom(true);
		return $m;
	}

	renderSteps($m, steps, live) {
		const $s = $m.find(".fai-steps");
		if (!steps || !steps.length) {
			$s.prop("hidden", true);
			return;
		}
		const running = steps.find((s) => s.status === "running");
		const done = steps.filter((s) => s.status !== "running").length;
		const list = steps
			.map(
				(s) =>
					`<li class="fai-step-${s.status}">${s.status === "running" ? '<span class="fai-spin"></span>' : s.status === "error" ? "⚠" : "✓"} ${esc(s.label)}</li>`
			)
			.join("");
		const head = live && running ? `<span class="fai-spin"></span> ${esc(running.label)}…` : __("Checked {0} source(s)", [done]);
		const open = $s.find("details").prop("open");
		$s.html(`<details${open ? " open" : ""}><summary>${head}</summary><ul>${list}</ul></details>`).prop("hidden", false);
	}

	renderText($m, text, streaming) {
		const $t = $m.find(".fai-text");
		if (!text) {
			if (!streaming) $t.html("");
			return;
		}
		$t.html(renderMarkdown(text) + (streaming ? '<span class="fai-caret"></span>' : ""));
		if (!streaming) this.enhance($t);
	}

	enhance($t) {
		$t.find(".fai-chart").each((_, el) => this.renderChart(el));
		$t.find(".fai-table-wrap").each((_, el) => {
			const $tools = $(`<div class="fai-table-tools"><button data-t="copy">${__("Copy")}</button><button data-t="csv">CSV</button></div>`);
			$(el).before($tools);
			$tools.on("click", "button", (e) => {
				const rows = [...el.querySelectorAll("tr")].map((tr) => [...tr.children].map((td) => td.innerText.trim()));
				if ($(e.currentTarget).data("t") === "copy") {
					this.copy(rows.map((r) => r.join("\t")).join("\n"));
				} else {
					const csv = rows.map((r) => r.map((c) => `"${c.replace(/"/g, '""')}"`).join(",")).join("\n");
					const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
					const a = document.createElement("a");
					a.href = URL.createObjectURL(blob);
					a.download = "fibersoft-ai-table.csv";
					document.body.appendChild(a);
					a.click();
					setTimeout(() => {
						URL.revokeObjectURL(a.href);
						a.remove();
					}, 500);
				}
			});
		});
	}

	renderChart(el) {
		let spec;
		try {
			spec = JSON.parse(el.getAttribute("data-chart"));
		} catch (e) {
			el.outerHTML = `<pre class="fai-code"><code>${esc(el.getAttribute("data-chart") || "")}</code></pre>`;
			return;
		}
		const type = ["bar", "line", "pie", "percentage", "donut"].includes(spec.type) ? spec.type : "bar";
		const datasets = (spec.datasets || []).map((d) => ({ name: String(d.name || ""), values: (d.values || []).map((v) => Number(v) || 0) }));
		if (!spec.labels || !datasets.length) {
			el.remove();
			return;
		}
		if (spec.title) $(el).before(`<div class="fai-chart-title">${esc(spec.title)}</div>`);
		try {
			const accent = this.cfg.theme_color || "#111111";
			// eslint-disable-next-line no-new
			new frappe.Chart(el, {
				data: { labels: spec.labels.map(String), datasets },
				type,
				height: 230,
				colors: [accent, "#6b7280", "#a3a3a3", "#374151", "#d4d4d4", "#525252", "#9ca3af"],
				axisOptions: { xIsSeries: type === "line", shortenYAxisNumbers: datasets.some((d) => d.values.some((v) => Math.abs(v) >= 10000)) ? 1 : 0 },
				tooltipOptions: { formatTooltipY: (d) => (typeof d === "number" ? format_number(d, null, 2) : d) },
			});
		} catch (e) {
			el.innerHTML = `<div class="fai-muted">${__("Could not draw chart")}</div>`;
		}
	}

	finishAssistant($m, state) {
		$m.find(".fai-typing").remove();
		this.renderSteps($m, state.steps, false);
		if (state.error) {
			$m.find(".fai-text").html(`<div class="fai-error">${esc(state.error)}</div>`);
		} else {
			this.renderText($m, state.text || "", false);
			if (!state.text && !(state.actions || []).length) $m.find(".fai-text").html(`<span class="fai-muted">${__("(no answer)")}</span>`);
		}
		this.renderActions($m, state.actions || []);
		const $f = $m.find(".fai-msg-foot").empty();
		if (state.message) {
			$m.attr("data-message", state.message);
			$f.append(`<button class="fai-foot-btn" data-f="copy" title="${__("Copy")}">${ICONS.copy}</button>`);
			$f.append(`<button class="fai-foot-btn${state.feedback === "Good" ? " on" : ""}" data-f="Good" title="${__("Helpful")}">${ICONS.up}</button>`);
			$f.append(`<button class="fai-foot-btn${state.feedback === "Bad" ? " on" : ""}" data-f="Bad" title="${__("Not helpful")}">${ICONS.down}</button>`);
			if (state.error && state.retry_text) $f.append(`<button class="fai-foot-btn" data-f="retry" title="${__("Retry")}">${ICONS.retry}</button>`);
			if (state.usage && state.usage.billable_tokens) {
				const cost = this.cfg.is_manager ? ` · $${Number(state.usage.cost_usd || 0).toFixed(4)}` : "";
				$f.append(`<span class="fai-usage" title="${__("Counted tokens for this answer")}">${format_number(state.usage.billable_tokens, null, 0)} ${__("tokens")}${cost}</span>`);
			}
			$f.prop("hidden", false);
			$f.off("click").on("click", "button", (e) => {
				const f = $(e.currentTarget).data("f");
				if (f === "copy") return this.copy(state.text || "");
				if (f === "retry") {
					this.$input.val(state.retry_text);
					return this.send();
				}
				const btn = $(e.currentTarget);
				const turnOff = btn.hasClass("on");
				const send = (note) =>
					call("set_feedback", { message: state.message, feedback: turnOff ? "" : f, note }).then(() => {
						$f.find('[data-f="Good"],[data-f="Bad"]').removeClass("on");
						if (!turnOff) btn.addClass("on");
					});
				if (f === "Bad" && !turnOff) {
					frappe.prompt(
						{ fieldtype: "Small Text", label: __("What was wrong? (optional)"), fieldname: "note" },
						(v) => send(v.note),
						__("Feedback"),
						__("Send")
					);
				} else send();
			});
		}
		this.scrollBottom();
	}

	// ------------------------------------------------------------------ actions
	renderActions($m, actions) {
		const $a = $m.find(".fai-actions");
		const existing = new Set($a.children().map((_, el) => el.dataset.action).get());
		actions.forEach((a) => {
			if (existing.has(a.name)) {
				this.updateActionCard($a.find(`[data-action="${a.name}"]`), a);
				return;
			}
			const $card = $(`<div class="fai-action" data-action="${esc(a.name)}"></div>`);
			$a.append($card);
			this.updateActionCard($card, a);
		});
	}

	previewHtml(preview) {
		if (!preview || typeof preview !== "object") return "";
		const val = (v) => {
			if (Array.isArray(v)) {
				if (v.length && typeof v[0] === "object") {
					const keys = [...new Set(v.flatMap((r) => Object.keys(r)))].slice(0, 6);
					return `<table class="fai-mini"><tr>${keys.map((k) => `<th>${esc(k)}</th>`).join("")}</tr>${v
						.slice(0, 20)
						.map((r) => `<tr>${keys.map((k) => `<td>${esc(r[k] ?? "")}</td>`).join("")}</tr>`)
						.join("")}</table>`;
				}
				return esc(v.join(", "));
			}
			if (v && typeof v === "object") {
				if ("from" in v || "to" in v) return `<span class="fai-from">${esc(v.from ?? "—")}</span> → <b>${esc(v.to ?? "—")}</b>`;
				return `<div class="fai-kv">${Object.entries(v).map(([k, x]) => `<div><span>${esc(k)}</span><div>${val(x)}</div></div>`).join("")}</div>`;
			}
			return esc(typeof v === "number" ? format_number(v, null, 2) : v ?? "");
		};
		const parts = [];
		Object.entries(preview).forEach(([k, v]) => {
			if (["values", "changes", "calculated", "calculated_after_change"].includes(k) && v && typeof v === "object") {
				const title = { values: __("Values"), changes: __("Changes"), calculated: __("Calculated by system"), calculated_after_change: __("After change") }[k];
				parts.push(`<div class="fai-preview-sec"><div class="fai-preview-title">${title}</div>${val(v)}</div>`);
			} else {
				parts.push(`<div class="fai-kv"><div><span>${esc(frappe.unscrub ? frappe.unscrub(k) : k)}</span><div>${val(v)}</div></div></div>`);
			}
		});
		return parts.join("");
	}

	updateActionCard($card, a) {
		const statusClass = { Pending: "pending", Executed: "done", Rejected: "muted", Failed: "failed", Expired: "muted", Undone: "muted" }[a.status] || "pending";
		const gone = a.action_type === "Create" && a.status === "Undone";
		const link = a.url && a.reference_name && !gone ? `<a href="${esc(a.url)}" data-fai-internal="1">${esc(a.reference_name)}</a>` : esc(a.reference_name || "");
		let buttons = "";
		if (a.status === "Pending") {
			buttons = `<button class="fai-btn fai-btn-primary" data-do="execute">${__("Confirm")}</button><button class="fai-btn" data-do="reject">${__("Reject")}</button>`;
		} else if (a.status === "Executed" && a.undoable) {
			buttons = `<button class="fai-btn" data-do="undo">${__("Undo")}</button>`;
		}
		$card.attr("class", `fai-action fai-action-${statusClass}`).html(`
			<div class="fai-action-head">
				<span class="fai-badge">${esc(ACTION_LABELS[a.action_type] || a.action_type)}</span>
				<span class="fai-action-dt">${esc(__(a.reference_doctype || ""))} ${link}</span>
				<span class="fai-action-status">${esc(__(a.status))}</span>
			</div>
			<div class="fai-action-summary">${esc(a.summary || "")}</div>
			${a.status === "Pending" ? `<div class="fai-action-preview">${this.previewHtml(a.preview)}</div>` : ""}
			${a.warnings && a.status === "Pending" ? `<div class="fai-action-notes">${esc(a.warnings)}</div>` : ""}
			${a.result || a.error ? `<div class="fai-action-result">${esc(a.error || a.result)}</div>` : ""}
			${buttons ? `<div class="fai-action-btns">${buttons}</div>` : ""}`);
		$card.find("[data-do]").on("click", (e) => this.actOn($card, a, $(e.currentTarget).data("do")));
	}

	actOn($card, a, what) {
		const run = () => {
			$card.find("button").prop("disabled", true);
			const method = { execute: "execute_action", reject: "reject_action", undo: "undo_action" }[what];
			call(method, { action: a.name })
				.then((view) => {
					this.updateActionCard($card, view);
					if (what === "execute" && view.status === "Executed") {
						frappe.show_alert({ message: view.result || __("Done"), indicator: "green" });
						if (window.cur_frm && cur_frm.doctype === view.reference_doctype && cur_frm.docname === view.reference_name) cur_frm.reload_doc();
						if (window.cur_list && cur_list.doctype === view.reference_doctype) cur_list.refresh();
					}
					if (view.status === "Failed") frappe.show_alert({ message: __("Action failed"), indicator: "red" });
				})
				.catch(() => $card.find("button").prop("disabled", false));
		};
		if (what === "execute" && ["Submit", "Cancel", "Workflow"].includes(a.action_type)) {
			frappe.confirm(__("Please confirm: {0}", [esc(a.summary || a.action_type)]), run);
		} else run();
	}

	// ------------------------------------------------------------------ send / stream
	send() {
		const text = (this.$input.val() || "").trim();
		if (!text || this.busy) return;
		if (!this.cfg.configured) {
			this.renderWelcome();
			return;
		}
		const context = this.includeContext ? this.currentContext() : null;
		this.$input.val("");
		this.autosize();
		this.addUser(text);
		const $m = this.addAssistant();
		this.setBusy(true);
		this.selection = null;
		this.renderSelChip();
		this.turn = { id: null, $m, text: "", lastEvent: 0, done: false, question: text };

		call("send_message", { message: text, conversation: this.conversation, context: context ? JSON.stringify(context) : null })
			.then((r) => {
				this.conversation = r.conversation;
				this.loadedConversation = r.conversation;
				storage(`fai:conv:${this.user}`, r.conversation);
				this.turn.id = r.turn_id;
				if (r.mode === "sync") return this.finishTurn(r.state);
				this.startPolling();
			})
			.catch((err) => {
				let msg = __("Could not send your message.");
				try {
					const sm = JSON.parse(err?._server_messages || "[]");
					if (sm.length) msg = stripHtml(JSON.parse(sm[0]).message);
				} catch (e) {
					/* keep default */
				}
				this.finishAssistant($m, { error: msg, steps: [] });
				this.setBusy(false);
				this.turn = null;
				this.refreshUsage();
			});
	}

	setBusy(busy) {
		this.busy = busy;
		this.$send.html(busy ? ICONS.stop : ICONS.send).attr("title", busy ? __("Stop") : __("Send")).toggleClass("fai-stop", busy);
		this.$root.find(".fai-status-dot").toggleClass("fai-thinking", busy);
	}

	stop() {
		if (this.turn && this.turn.id) call("cancel_turn", { turn_id: this.turn.id });
	}

	startPolling() {
		clearInterval(this.pollTimer);
		const started = Date.now();
		this.pollTimer = setInterval(() => {
			const t = this.turn;
			if (!t || t.done) return clearInterval(this.pollTimer);
			if (Date.now() - t.lastEvent < 2500) return; // realtime is flowing
			if (Date.now() - started > 15 * 60 * 1000) {
				clearInterval(this.pollTimer);
				return this.finishTurn({ status: "error", error: __("The answer took too long. Please try again."), steps: [] });
			}
			call("get_turn", { turn_id: t.id }, { type: "GET" })
				.then((state) => {
					if (!state || this.turn !== t || t.done) return;
					if (state.status === "done" || state.status === "error") this.finishTurn(state);
					else this.applyProgress(state);
				})
				.catch(() => {});
		}, POLL_MS);
	}

	onRealtime(data) {
		const t = this.turn;
		if (!t || !data || data.turn_id !== t.id || t.done) return;
		t.lastEvent = Date.now();
		if (data.type === "delta") this.applyProgress({ text: data.text });
		else if (data.type === "step") this.applyProgress({ steps: data.steps, actions: data.actions });
		else if (data.type === "done") this.finishTurn(data.state);
	}

	applyProgress(state) {
		const t = this.turn;
		if (!t) return;
		if (state.steps) this.renderSteps(t.$m, state.steps, true);
		if (state.actions && state.actions.length) this.renderActions(t.$m, state.actions);
		if (typeof state.text === "string" && state.text.length >= t.text.length && state.text !== t.text) {
			t.text = state.text;
			if (!t.raf) {
				t.raf = requestAnimationFrame(() => {
					t.raf = null;
					if (t.done) return;
					t.$m.find(".fai-typing").remove();
					this.renderText(t.$m, t.text, true);
					this.scrollBottom();
				});
			}
		}
	}

	finishTurn(state) {
		const t = this.turn;
		if (!t || t.done) return;
		t.done = true;
		clearInterval(this.pollTimer);
		state = state || {};
		this.finishAssistant(t.$m, {
			text: state.text || t.text,
			error: state.status === "error" ? state.error || __("Something went wrong") : null,
			actions: state.actions || [],
			steps: state.steps || [],
			message: state.message,
			usage: state.usage,
			retry_text: t.question,
		});
		this.setBusy(false);
		this.turn = null;
		this.refreshUsage();
		if (this.$panel.prop("hidden")) this.$root.find(".fai-launcher-badge").prop("hidden", false);
	}

	// ------------------------------------------------------------------ token allocation meter
	refreshUsage() {
		call("get_my_usage", {}, { type: "GET" })
			.then((st) => this.renderUsage(st))
			.catch(() => {});
	}

	renderUsage(st) {
		const $m = this.$root.find(".fai-meter");
		if (!st || !st.has_limits) {
			$m.prop("hidden", true);
			return;
		}
		const names = { daily: __("Today"), weekly: __("This week"), monthly: __("This month") };
		// show the allocation closest to running out
		const [key, e] = Object.entries(st.periods)
			.filter(([, v]) => v.limit)
			.sort((a, b) => b[1].percent - a[1].percent)[0];
		const pct = Math.min(e.percent, 100);
		const fmt = (n) => format_number(n || 0, null, 0);
		$m.toggleClass("fai-meter-warn", pct >= 75 && pct < 100).toggleClass("fai-meter-full", pct >= 100);
		$m.find(".fai-meter-text").html(
			pct >= 100
				? __("{0}: token allocation used up. Resets {1}.", [names[key], esc(e.resets)])
				: __("{0}: {1} of {2} tokens used ({3}%)", [names[key], fmt(e.used), fmt(e.limit), e.percent])
		);
		$m.find(".fai-meter-bar i").css("width", pct + "%");
		$m.attr("title", Object.entries(st.periods).filter(([, v]) => v.limit).map(([k, v]) => `${names[k]}: ${fmt(v.used)} / ${fmt(v.limit)}`).join("\n"));
		$m.prop("hidden", false);
	}

	// ------------------------------------------------------------------ voice
	toggleVoice() {
		if (this.recognition) return this.stopVoice();
		const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
		if (!SR) return;
		const rec = new SR();
		const lang = (frappe.boot.lang || "en").toLowerCase();
		rec.lang = lang.startsWith("ur") ? "ur-PK" : navigator.language || "en-US";
		rec.interimResults = true;
		rec.continuous = false;
		const base = this.$input.val() ? this.$input.val() + " " : "";
		rec.onresult = (e) => {
			let text = "";
			for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
			this.$input.val(base + text);
			this.autosize();
		};
		rec.onend = () => this.stopVoice();
		rec.onerror = () => this.stopVoice();
		this.recognition = rec;
		this.$root.find(".fai-mic").addClass("fai-recording");
		rec.start();
	}

	stopVoice() {
		if (this.recognition) {
			try {
				this.recognition.stop();
			} catch (e) {
				/* ignore */
			}
		}
		this.recognition = null;
		this.$root.find(".fai-mic").removeClass("fai-recording");
	}

	copy(text) {
		const done = () => frappe.show_alert({ message: __("Copied"), indicator: "green" }, 2);
		if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
		else {
			const ta = document.createElement("textarea");
			ta.value = text;
			document.body.appendChild(ta);
			ta.select();
			document.execCommand("copy");
			ta.remove();
			done();
		}
	}
}
