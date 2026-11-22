// Safe, dependency-free markdown renderer for assistant answers.
// Everything is HTML-escaped first; only a known subset of markdown becomes markup.

const esc = (s) =>
	String(s)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");

function safeUrl(url) {
	const u = String(url || "").trim().replace(/&amp;/g, "&");
	if (/^\/(?!\/)/.test(u)) return u; // site-relative, e.g. /app/sales-invoice/X
	if (/^https?:\/\//i.test(u)) return u;
	if (/^mailto:/i.test(u)) return u;
	return null;
}

function inline(text) {
	// text is already escaped
	const codes = [];
	let s = text.replace(/`([^`\n]+)`/g, (_, c) => {
		codes.push(c);
		return `\u0000${codes.length - 1}\u0000`;
	});
	const links = [];
	s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+(?:\s[^)]*)?)\)/g, (m, label, url) => {
		const href = safeUrl(url.replace(/%20/g, " "));
		if (!href) return label;
		const internal = href.startsWith("/");
		links.push(
			`<a href="${esc(href)}"${internal ? ' data-fai-internal="1"' : ' target="_blank" rel="noopener noreferrer"'}>${label}</a>`
		);
		return `\u0002${links.length - 1}\u0002`;
	});
	s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
	s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
	s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
	s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
	s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
	s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
	s = s.replace(/\u0002(\d+)\u0002/g, (_, i) => links[+i]);
	return s;
}

function splitRow(line) {
	let l = line.trim();
	if (l.startsWith("|")) l = l.slice(1);
	if (l.endsWith("|")) l = l.slice(0, -1);
	return l.split("|").map((c) => c.trim());
}

const isTableSep = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

export function renderMarkdown(src) {
	const blocks = [];
	let text = String(src || "").replace(/\r\n/g, "\n");

	// fenced code (incl. fai-chart). An unterminated fence while streaming is shown as plain code.
	text = text.replace(/```([\w-]*)\n([\s\S]*?)(```|$)/g, (m, lang, code, close) => {
		blocks.push({ lang: lang.toLowerCase(), code, closed: close === "```" });
		return `\n\u0001${blocks.length - 1}\u0001\n`;
	});

	const lines = esc(text).split("\n");
	const out = [];
	let i = 0;
	let para = [];
	const flush = () => {
		if (para.length) {
			out.push(`<p>${inline(para.join("<br>"))}</p>`);
			para = [];
		}
	};

	while (i < lines.length) {
		const line = lines[i];
		const block = line.match(/^\u0001(\d+)\u0001$/);
		if (block) {
			flush();
			const b = blocks[+block[1]];
			if (b.lang === "fai-chart") {
				out.push(
					b.closed
						? `<div class="fai-chart" data-chart="${esc(b.code)}"></div>`
						: `<div class="fai-chart-loading">${esc(__("Preparing chart..."))}</div>`
				);
			} else {
				out.push(`<pre class="fai-code"><code>${esc(b.code)}</code></pre>`);
			}
			i++;
			continue;
		}
		if (!line.trim()) {
			flush();
			i++;
			continue;
		}
		const h = line.match(/^(#{1,4})\s+(.*)$/);
		if (h) {
			flush();
			const lvl = Math.min(h[1].length + 2, 6);
			out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
			i++;
			continue;
		}
		if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
			flush();
			out.push("<hr>");
			i++;
			continue;
		}
		if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
			flush();
			const head = splitRow(line);
			const aligns = splitRow(lines[i + 1]).map((c) =>
				c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""
			);
			i += 2;
			const rows = [];
			while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
				rows.push(splitRow(lines[i]));
				i++;
			}
			const al = (k) => (aligns[k] ? ` style="text-align:${aligns[k]}"` : "");
			out.push(
				`<div class="fai-table-wrap"><table class="fai-table"><thead><tr>${head
					.map((c, k) => `<th${al(k)}>${inline(c)}</th>`)
					.join("")}</tr></thead><tbody>${rows
					.map((r) => `<tr>${head.map((_, k) => `<td${al(k)}>${inline(r[k] ?? "")}</td>`).join("")}</tr>`)
					.join("")}</tbody></table></div>`
			);
			continue;
		}
		if (/^\s*&gt;\s?/.test(line)) {
			flush();
			const q = [];
			while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
				q.push(lines[i].replace(/^\s*&gt;\s?/, ""));
				i++;
			}
			out.push(`<blockquote>${inline(q.join("<br>"))}</blockquote>`);
			continue;
		}
		const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
		if (li) {
			flush();
			const ordered = /\d/.test(li[2]);
			const items = [];
			while (i < lines.length) {
				const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
				if (m) {
					items.push({ indent: m[1].length, text: m[3] });
					i++;
				} else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
					items[items.length - 1].text += "<br>" + lines[i].trim();
					i++;
				} else break;
			}
			const tag = ordered ? "ol" : "ul";
			out.push(
				`<${tag}>${items
					.map((it) => `<li${it.indent >= 2 ? ' class="fai-li-nested"' : ""}>${inline(it.text)}</li>`)
					.join("")}</${tag}>`
			);
			continue;
		}
		para.push(line);
		i++;
	}
	flush();
	return out.join("");
}

export function escapeHtml(s) {
	return esc(s);
}
