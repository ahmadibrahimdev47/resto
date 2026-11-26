// Restaurant floor: halls and tables, takeaway/delivery orders, order taking and settling.
// Plain JS on purpose (no build step); every action is checked again on the server.

const API = "resto.resto.floor.";
const COURSES = ["", "Starter", "Main Course", "Dessert", "Drinks"];

frappe.pages["resto-floor"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Restaurant Floor"), single_column: true });
	wrapper.resto_floor = new RestoFloor(page, wrapper);
};

frappe.pages["resto-floor"].on_page_show = function (wrapper) {
	wrapper.resto_floor && wrapper.resto_floor.refresh();
};

// A failed call rejects with {network}: true when the server was not reached (no connection, the
// server down or too slow, the session expired), so the change can be sent again; false when the
// server refused it. Frappe calls no error callback on some failures: "always" settles those.
function call(method, args, freeze, timeout) {
	return new Promise((resolve, reject) => {
		let req = null;
		let settled = false;
		let timer = null;
		const done = (fn, value) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			fn(value);
		};
		// Refused: the server answered with its own error (a message or an exception) that sending
		// again will not change. Anything else (no answer, a proxy or portal page, a rate limit, an
		// expired session, a deadlock, a timeout) is "not reached": the change is kept and sent again.
		const failed = () => {
			const status = req ? req.status : 0;
			const json = (req && req.responseJSON) || {};
			const exc = String(json.exc_type || "");
			const expired =
				!!json.session_expired ||
				!!(frappe.app && frappe.app.session_expired_dialog && frappe.app.session_expired_dialog.display) ||
				(frappe.request && frappe.request.is_session_expired && frappe.request.is_session_expired(json));
			const refused =
				!!(json.exc_type || json._server_messages || json.exc) &&
				![401, 408, 429].includes(status) &&
				status < 500 &&
				!["QueryDeadlockError", "QueryTimeoutError", "CSRFTokenError", "SessionExpired"].includes(exc);
			done(reject, { network: !refused || !!expired, status });
		};
		if (timeout)
			timer = setTimeout(() => {
				done(reject, { network: true, status: 0 });
				// Given up: stopped here (the server may still finish it; an op makes a retry harmless).
				if (req && typeof req.abort === "function") req.abort();
			}, timeout);
		req = frappe.call({
			method: API + method,
			args: args || {},
			freeze: !!freeze,
			callback: (r) => done(resolve, r.message),
			error: failed,
			always: () => setTimeout(failed, 0),
		});
	});
}

// The server itself answered, busy or failing, without refusing (5xx, a rate limit, a timeout):
// the connection is there and the change is sent again.
const server_busy = (e) => {
	const status = cint(e && e.status);
	return status >= 500 || status === 429 || status === 408;
};

// "9:05:00" (a time from the server) as "09:05".
const hhmm = (t) => {
	const [h, m] = String(t || "").split(":");
	return m === undefined ? "" : `${h.padStart(2, "0")}:${m}`;
};
const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));

// A total rounded as ERPNext rounds an invoice: to the currency's smallest fraction when it has
// one, else to a whole unit (an exact half to the even one, as Frappe's default rounding).
function round_money(value, fraction, method) {
	const v = flt(value);
	if (fraction > 0) {
		const rem = Math.round((v % fraction) * 1e6) / 1e6;
		const out = rem > fraction / 2 ? v + (fraction - rem) : v - rem;
		return sys_round(out, 2, method);
	}
	return sys_round(v, 0, method);
}

// Frappe's rounding to a number of places (System Settings "Rounding Method"): a half goes to the
// even digit, or away from zero with Commercial Rounding.
function sys_round(value, places, method) {
	const m = Math.pow(10, places || 0);
	const n = flt(value) * m;
	const down = Math.floor(n);
	const diff = Math.round((n - down) * 1e6) / 1e6;
	let out;
	if (diff > 0.5) out = down + 1;
	else if (diff < 0.5) out = down;
	else if (method === "Commercial Rounding") out = n >= 0 ? down + 1 : down;
	else out = down % 2 === 0 ? down : down + 1;
	return out / m;
}

// A manager's approval for what a waiter or cashier cannot do alone (D-204): a manager on this
// screen needs none ({}); otherwise the manager chooses their name and types their PIN here. The
// server checks it inside the action itself, for that action and amount only. null: not given.
function ask_approval(floor, what) {
	if (floor.roles.manager) return Promise.resolve({});
	return new Promise(async (resolve) => {
		const list = await frappe.xcall("resto.resto.approval.approvers");
		if (!list.length) {
			frappe.msgprint(__("No manager has an approval PIN yet: a manager sets it from My Approval PIN on this screen"));
			return resolve(null);
		}
		let done = false;
		const d = new frappe.ui.Dialog({
			title: __("Manager Approval"),
			fields: [
				{ fieldname: "what", fieldtype: "HTML", options: `<p><b>${esc(what)}</b></p>` },
				{
					fieldname: "approver",
					fieldtype: "Select",
					label: __("Manager"),
					reqd: 1,
					options: list.map((m) => ({ value: m.user, label: m.full_name })),
					default: list[0].user,
				},
				{ fieldname: "pin", fieldtype: "Password", label: __("PIN"), reqd: 1 },
			],
			primary_action_label: __("Approve"),
			primary_action: (v) => {
				done = true;
				d.hide();
				resolve({ approver: v.approver, pin: v.pin });
			},
			on_hide: () => !done && resolve(null),
		});
		d.show();
		// A PIN, not a saved password: the browser must not offer to remember it.
		d.$wrapper.find("input[data-fieldname=pin]").attr({ autocomplete: "off", inputmode: "numeric" });
	});
}
const money = (v) => format_currency(v || 0);

// A line's sides text with its «without» part red, bold and larger (D-251), on a line of its own
// under the rest (D-263), escaped. The text was written in the site's language; the cashier's
// screen is in it too.
function sides_paper(text) {
	const head = __("Sides: {0}", [""], "Resto");
	const without = __("without: {0}", [""], "Resto").trim();
	const start = head && text.startsWith(head) ? head : "";
	const parts = text.slice(start.length).split(" | ");
	const left_out = parts.filter((p) => without && p.startsWith(without));
	const rest = parts.filter((p) => !left_out.includes(p));
	const lines = rest.length ? [esc(start) + rest.map(esc).join(" | ")] : [];
	// Only what is left out: its own line, without an empty «with» before it.
	return lines.concat(left_out.map((p) => `<span class="w">${esc(p)}</span>`)).join("<br>");
}

// A slot served as one portion per dish, chosen as one of its options or none (D-268): a side
// group of max 1 on one dish, not a choice shared with other dishes.
const one_of = (r, s) => !!r.per_dish && !s.single && !s.shared && cint(s.max) === 1;

// What Net In for the Shift is made of (D-235, D-245): only the parts the shift has.
function net_caption(s) {
	let text = __("Collected");
	if (s.expenses_count) text += " − " + __("Expenses");
	if (s.purchases_count) text += " − " + __("Purchases");
	if (s.advances_count) text += " − " + __("Advances", null, "Resto");
	if (s.cash_in_count) text += " + " + __("Cash In", null, "Resto");
	if (s.drops_count) text += " − " + __("Drops", null, "Resto");
	return text;
}

// How a payment method's expected amount is made (D-236), from the parts the server worked out:
// opening and sales always, the others when there are any, then the expected amount.
function expected_parts(p) {
	const parts = [
		[__("Opening (starting change)"), flt(p.opening_amount), 1],
		[__("Sales paid with this method"), flt(p.sales_amount), 1],
		[__("Debts collected"), flt(p.debts_amount), 1],
		[__("Drawer Running Expenses"), flt(p.expenses_amount), -1],
		[__("Drawer Stock Purchases"), flt(p.purchases_amount), -1],
		[__("Staff Advances"), flt(p.advances_amount), -1],
		[__("Cash In to the Drawer"), flt(p.cash_in_amount), 1],
		[__("Cash Drops to the Main Safe"), flt(p.drops_amount), -1],
		[__("Paid back to customers"), flt(p.refunds_amount), -1],
	].filter(([, v], i) => i < 2 || Math.abs(v) >= 0.005);
	const line = (label, value, cls = "") =>
		`<div class="rf-xline ${cls}"><span>${label}</span><span class="rf-amt">${value}</span></div>`;
	return (
		parts.map(([label, v, sign]) => line(label, `${sign * v < 0 ? "−" : "+"}${money(Math.abs(v))}`)).join("") +
		line(`= ${__("Expected")}`, money(p.expected_amount), "rf-xtotal")
	);
}

// Before money is posted from a shift dialog (D-230): what will be posted is said back, line by line
// (already escaped); No leaves the dialog as it was filled. true: go ahead.
function confirm_post(lines) {
	return new Promise((resolve) =>
		frappe.confirm(
			`${lines.join("")}<p><b>${__("Do you want to continue?", null, "Resto")}</b></p>`,
			() => resolve(true),
			() => resolve(false)
		)
	);
}
const fmt_stock_qty = (q) => String(flt(q, 6));

// The floor's look (D-214): line icons drawn inline (no icon font to load), sized by CSS.
const svg = (d, fill) =>
	`<svg viewBox="0 0 24 24" fill="${fill ? "currentColor" : "none"}" stroke="${fill ? "none" : "currentColor"}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
	lock: svg('<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'),
	bell: svg('<path d="M4 15h16"/><path d="M6 15a6 6 0 0 1 12 0"/><path d="M12 7V5"/><path d="M3 19h18"/>'),
	clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
	wifi: svg('<path d="M5 12.5a10 10 0 0 1 14 0"/><path d="M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1"/>'),
	refresh: svg('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>'),
	receipt: svg('<path d="M7 3h10v18l-2.5-1.5L12 21l-2.5-1.5L7 21z"/><path d="M10 8h4M10 12h4"/>'),
	drawer: svg('<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5h8v2"/><path d="M3 12h18"/>'),
	truck: svg('<path d="M3 7h11v9H3z"/><path d="M14 10h4l3 3v3h-7"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/>'),
	plus: svg('<path d="M12 5v14M5 12h14"/>'),
	minus: svg('<path d="M5 12h14"/>'),
	back: svg('<path d="M9 6l6 6-6 6"/>'),
	user: svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
	search: svg('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>'),
	barcode: svg('<path d="M4 7V5h3M17 5h3v2M20 17v2h-3M7 19H4v-2"/><path d="M8 9v6M11 9v6M14 9v6M17 9v6"/>'),
	more: svg('<circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/>', true),
	trash: svg('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
	send: svg('<path d="M4 12l16-8-6 16-2-6z"/>'),
	print: svg('<path d="M6 9V3h12v6"/><rect x="4" y="9" width="16" height="8" rx="2"/><path d="M7 14h10v7H7z"/>'),
	spin: svg('<path d="M21 12a9 9 0 1 1-3-6.7"/>'),
	tag: svg('<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.5"/>'),
	gift: svg('<rect x="3" y="8" width="18" height="5" rx="1"/><path d="M5 13v8h14v-8M12 8v13"/><path d="M12 8c-2-4-6-4-6-1.5S9 8 12 8c3 0 6 .5 6-1.5S14 4 12 8z"/>'),
	swap: svg('<path d="M7 4L3 8l4 4"/><path d="M3 8h14"/><path d="M17 20l4-4-4-4"/><path d="M21 16H7"/>'),
};
// Minutes as "38 min" or "1 h 05 min".
const dur = (minutes) => {
	const m = Math.max(cint(minutes), 0);
	const h = Math.floor(m / 60);
	return h ? __("{0} h {1} min", [h, String(m % 60).padStart(2, "0")]) : __("{0} min", [m]);
};
const minutes_since = (dt) => (dt ? Math.max(0, Math.floor((Date.now() - frappe.datetime.str_to_obj(dt).getTime()) / 60000)) : 0);
const fmt_qty = (q) => (flt(q) % 1 ? flt(q).toFixed(2).replace(/0$/, "") : String(cint(q)));

// Changes made on the order screen and not yet saved on the server (D-213): kept in this browser,
// per user, so a dropped connection or a reload does not lose them, and sent in order when the
// server answers again. Each has an id the server keeps with it, so sending it twice adds nothing.
let MEMORY_OUTBOX = [];
let STORAGE_BROKEN = false;
const outbox_key = () => `resto_floor_outbox|${frappe.session.user}`;
function outbox() {
	if (STORAGE_BROKEN) return MEMORY_OUTBOX;
	try {
		const list = JSON.parse(localStorage.getItem(outbox_key()) || "[]");
		return Array.isArray(list) ? list : [];
	} catch (e) {
		return MEMORY_OUTBOX; // storage off (a private window): kept while the page is open
	}
}
function set_outbox(list) {
	MEMORY_OUTBOX = list;
	try {
		localStorage.setItem(outbox_key(), JSON.stringify(list));
	} catch (e) {
		STORAGE_BROKEN = true;
		// memory only
	}
}
const plain_line = (d) =>
	d.kitchen_status === "Not Sent" &&
	(!d.modifiers || d.modifiers === "[]") &&
	!(d.notes || "").trim() &&
	!d.course &&
	!d.hold &&
	!d.sides &&
	!d.sides_extras &&
	!flt(d.billed_qty);

class RestoFloor {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.hall = localStorage.getItem("resto_floor_hall") || null;
		this.floor = null;
		this.menu = null;
		this.order = null;
		this._gen = 0; // bumped by every change made on the screen
		this.make();
		this.refresh();
		frappe.realtime.on("resto_update", () => this.schedule_refresh());
		$(window).on("online", () => {
			this.drain();
			this.refresh();
		});
		$(window).on("offline", () => this.set_offline(true));
		if (outbox().length) this.drain();
		this._last_act = Date.now();
		// A touch on any tab of this browser counts: a floor tab left behind never locks the one in use.
		const act = () => {
			const now = Date.now();
			this._last_act = now;
			if (now - (this._act_saved || 0) < 5000) return;
			this._act_saved = now;
			try {
				localStorage.setItem(`resto_floor_active|${frappe.session.user}`, String(now));
			} catch (e) {
				// this tab's own time only
			}
		};
		["pointerdown", "keydown", "wheel", "touchstart"].forEach((t) => window.addEventListener(t, act, { capture: true, passive: true }));
		// Locked, opened or signed out in another tab of this browser: the same here. A lock waits
		// for a payment open here to finish (the clock shows it then).
		window.addEventListener("storage", (e) => {
			if (e.key === outbox_key()) return this.pending_gone();
			if (e.key !== this.lock_key()) return;
			if (e.newValue === "out") window.location.href = "/login";
			else if (this.lock_stored()) !this._paying && !this._settling && this.show_lock();
			else if (this._locked && !e.newValue) this.unlock_done();
		});
		if (this.lock_stored()) this.show_lock();
		else {
			try {
				localStorage.removeItem(this.lock_key()); // left by a session that ended
			} catch (e) {
				// nothing kept
			}
		}
	}

	make() {
		// The screen draws its own header (D-214): Frappe's page title bar is hidden on this page.
		$(this.wrapper).addClass("rf-page");
		$(this.page.body).html(`
			<style>${RestoFloor.css()}</style>
			<div class="rf-root">
				<div class="resto-floor">
					<header class="rf-head"></header>
					<div class="rf-alerts"></div>
					<div class="rf-bar"><div class="rf-tabs"></div><span class="rf-spacer"></span><div class="rf-acts"></div></div>
					<main class="rf-main"><section class="rf-body"></section><aside class="rf-side"></aside></main>
				</div>
				<div class="rf-order" style="display:none"></div>
			</div>`);
		const $b = $(this.page.body);
		this.$head = $b.find(".rf-head").first();
		this.$alerts = $b.find(".rf-alerts");
		this.$tabs = $b.find(".rf-tabs");
		this.$acts = $b.find(".rf-acts");
		this.$body = $b.find(".rf-body");
		this.$side = $b.find(".rf-side");
		this.$order = $b.find(".rf-order");
		this._clock = setInterval(() => this.tick(), 15000);
		// F4: the menu search (a barcode scanner types into it too).
		$(document).on("keydown.resto_floor", (e) => {
			if (e.key !== "F4" || this._locked || !this.$order.is(":visible")) return;
			e.preventDefault();
			this.$order.find(".rf-search").trigger("focus").select();
		});
	}

	role_label() {
		const r = this.floor.roles;
		if (r.manager) return __("Manager", null, "Resto");
		if (r.cashier) return __("Cashier", null, "Resto");
		if (r.waiter) return __("Waiter", null, "Resto");
		if (r.host) return __("Host", null, "Resto");
		return "";
	}

	// The header of the floor: the restaurant, the shift, the time, the connection, the user.
	render_head() {
		const f = this.floor;
		const initial = (f.user_name || "?").trim().charAt(0);
		this.put(this.$head, `
			<div class="rf-brand"><span class="rf-logo">${ICON.bell}</span>
				<div><b>${esc(f.brand || __("Restaurant Floor"))}</b><small>${__("Floor", null, "Resto")}</small></div></div>
			<span class="rf-spacer"></span>
			${this.shift_pill()}
			<span class="rf-pill rf-pill-gray rf-hide-sm">${ICON.clock}<span class="rf-clock-t"></span></span>
			${this.net_pill()}
			<button class="rf-ib" data-h="refresh" title="${__("Refresh")}" aria-label="${__("Refresh")}">${ICON.refresh}</button>
			<button class="rf-btn rf-userbtn" data-h="user"><span class="rf-av">${esc(initial)}</span>
				<span class="rf-hide-sm">${esc(f.user_name)}${this.role_label() ? ` · ${esc(this.role_label())}` : ""}</span></button>`, () => {
			this.$head.find("[data-h=refresh]").on("click", (e) => this.refresh_now($(e.currentTarget)));
			this.$head.find("[data-h=user]").on("click", () => this.user_menu());
			this.$head.find("[data-shift]").on("click", (e) => this.shift_click($(e.currentTarget).attr("data-shift")));
		});
		this.tick();
		if (this._locked) $(".rf-lock .rf-lock-brand").text(f.brand || "");
	}

	net_pill() {
		return this.offline
			? `<span class="rf-pill rf-pill-amber rf-net">${ICON.wifi}<span>${__("Offline", null, "Resto")}</span></span>`
			: `<span class="rf-pill rf-pill-gray rf-net ok">${ICON.wifi}<span class="rf-hide-sm">${__("Online", null, "Resto")}</span></span>`;
	}

	// The clock and the shift's time, every few seconds.
	tick() {
		const lang = `${frappe.boot.lang || "en"}-u-nu-latn`;
		const now = new Date();
		let text = "";
		try {
			text = `${now.toLocaleTimeString(lang, { hour: "numeric", minute: "2-digit" })} · ${now.toLocaleDateString(lang, { weekday: "long" })}`;
		} catch (e) {
			text = now.toTimeString().slice(0, 5);
		}
		$(this.page.body).find(".rf-clock-t").text(text);
		const s = this.floor && this.floor.shift;
		if (s && s.shift && s.since) $(this.page.body).find(".rf-shift-t").text(__("Shift open for {0}", [dur(minutes_since(s.since))]));
		// Left untouched (Resto Settings "Lock the Floor Screen After"): it locks itself. Not in the
		// middle of a payment, and not while it could not be opened again (offline: the PIN is checked
		// on the server) or while changes wait to be sent.
		if (!this._locked && !this._paying && !this._settling && this.lock_stored()) return this.show_lock();
		const m = this.floor && this.floor.screen_lock && cint(this.floor.lock_after_minutes);
		let last = this._last_act;
		try {
			last = Math.max(last, cint(localStorage.getItem(`resto_floor_active|${frappe.session.user}`)));
		} catch (e) {
			// this tab's own time only
		}
		const idle = Date.now() - last > m * 60000;
		if (m && idle && this.floor.has_pin && !this._locked && !this._settling && !this._paying && !this.offline && !outbox().length && $(this.wrapper).is(":visible")) {
			this.lock();
		}
	}

	schedule_refresh() {
		clearTimeout(this._t);
		this._t = setTimeout(() => this.refresh(), 400);
	}

	// The header's Refresh: the icon turns while the floor (and the open order) are read again, then
	// a short "Updated"; with no connection the screen says so as usual.
	async refresh_now($btn) {
		if ($btn.prop("disabled")) return;
		$btn.prop("disabled", true).addClass("rf-busy");
		const was_offline = this.offline;
		try {
			await Promise.all([this.refresh(), this.order ? this.reload_lines(this.order.name).catch(() => {}) : null]);
		} finally {
			$btn.prop("disabled", false).removeClass("rf-busy");
		}
		if (!this.offline) frappe.show_alert({ message: __("Updated"), indicator: "green" }, 2);
		else if (!was_offline) this.set_offline(true);
	}

	async refresh() {
		if (!$(this.wrapper).is(":visible")) return;
		const seq = (this._refresh_seq = (this._refresh_seq || 0) + 1);
		let floor;
		try {
			floor = await call("get_floor");
			if (seq !== this._refresh_seq) return; // a newer refresh was asked for meanwhile
			this.floor = floor;
		} catch (e) {
			if (e && e.network) {
				this.set_offline(!server_busy(e));
				// Not loaded yet: try again soon (once loaded, the periodic refresh does).
				if (!this._poll) this._t = setTimeout(() => this.refresh(), 10000);
			}
			return;
		}
		if (this.offline) this.set_offline(false);
		// The lock turned off in Resto Settings: a screen locked before opens.
		if (this._locked && !this.floor.screen_lock) this.unlock_done();
		if (outbox().length) this.drain();
		this.render_floor();
		if (this.order) this.reload_lines(this.order.name).catch(() => {});
		clearInterval(this._poll);
		this._poll = setInterval(() => this.refresh(), (this.floor.refresh_seconds || 15) * 1000);
	}

	// Puts html in a part of the floor only when it changed: a refresh every few seconds then keeps
	// the list's scroll and never replaces a button under the finger.
	put($el, html, bind) {
		if ($el.data("rf-html") === html) return;
		const top = $el.find(".rf-side-l").scrollTop();
		$el.html(html).data("rf-html", html);
		if (bind) bind();
		if (top) $el.find(".rf-side-l").scrollTop(top);
	}

	// ------------------------------------------------------------------ printing

	kitchen_print_alert() {
		// Kitchen printing that stopped is shown here too: the kitchen may not be looking at a screen.
		const k = this.floor.kitchen_print || {};
		const parts = [];
		if (k.unprinted) parts.push(__("Kitchen tickets not printed: {0}", [k.unprinted]));
		if ((k.no_screen || []).length) parts.push(__("No kitchen screen is printing for: {0}", [esc(k.no_screen.join("، "))]));
		return parts.length ? `<div class="rf-print-alert">⚠ ${parts.join(" · ")}</div>` : "";
	}

	prints_here() {
		// Bills and receipts print from the browser only on the device that prints them (the cashier's),
		// not on a waiter's tablet. Kept per device; a cashier's device prints by default.
		let v = null;
		try {
			v = localStorage.getItem("resto_prints_here");
		} catch (e) {
			v = null;
		}
		return v === null ? !!(this.floor && this.floor.roles.cashier) : v === "1";
	}

	// A manager's PIN for approvals on a waiter's or cashier's screen (D-204).
	// kind "approve": a manager's approval PIN. kind "lock": the PIN that opens this screen once
	// locked; a manager's is a different one (the approval PIN is typed on other people's screens).
	set_my_pin(kind, then_lock) {
		const manager = this.floor && this.floor.roles.manager;
		const help =
			kind === "approve"
				? __("Typed on a waiter's or cashier's screen to approve a void after sending, a cancel, a discount above the limit, or a bill on account. 4 to 8 digits; keep it to yourself.")
				: manager
				? __("Opens this screen after it is locked. 4 to 8 digits, different from your approval PIN; keep it to yourself.")
				: __("Opens this screen after it is locked. 4 to 8 digits; keep it to yourself.");
		const d = new frappe.ui.Dialog({
			title: then_lock ? __("Set Your PIN to Lock the Screen") : kind === "approve" ? __("My Approval PIN") : manager ? __("My Lock PIN") : __("My PIN"),
			fields: [
				{ fieldname: "help", fieldtype: "HTML", options: `<p class="text-muted">${help}</p>` },
				{ fieldname: "pin", fieldtype: "Password", label: __("New PIN"), reqd: 1 },
				{ fieldname: "again", fieldtype: "Password", label: __("New PIN Again"), reqd: 1 },
				{ fieldname: "password", fieldtype: "Password", label: __("Your Login Password"), reqd: 1 },
			],
			primary_action_label: __("Save"),
			primary_action: async (v) => {
				if (v.pin !== v.again) return frappe.msgprint(__("The two PINs are not the same"));
				await frappe.xcall("resto.resto.approval.set_my_pin", { pin: v.pin, password: v.password, kind });
				d.hide();
				frappe.show_alert({ message: __("Your PIN is saved"), indicator: "green" });
				if (this.floor && kind === "lock") this.floor.has_pin = 1;
				if (then_lock) this.lock();
			},
		});
		d.show();
		d.$wrapper.find("input[data-fieldname=pin], input[data-fieldname=again]").attr({ autocomplete: "new-password", inputmode: "numeric" });
		d.$wrapper.find("input[data-fieldname=password]").attr({ autocomplete: "current-password" });
	}

	// ------------------------------------------------------------------ lock screen (D-216)

	// The screen covers the page (and any window open on it) until its user types their own PIN,
	// checked on the server. A curtain for a shared counter: the session stays signed in under it.
	// Switch User signs out, and the next person signs in with their password.
	lock_key() {
		return `resto_floor_locked|${frappe.session.user}`;
	}

	// Kept with this session's token: a new sign-in (the password typed) is not asked for the PIN.
	lock_stored() {
		try {
			return !!frappe.csrf_token && localStorage.getItem(this.lock_key()) === frappe.csrf_token;
		} catch (e) {
			return false;
		}
	}

	async lock() {
		if (this._locked || !this.floor || !this.floor.screen_lock) return;
		if (!this.floor.has_pin) return this.set_my_pin("lock", true);
		if (this.offline || navigator.onLine === false) {
			return frappe.msgprint(__("Not connected: the screen locks when the connection is back (its PIN is checked on the server)."));
		}
		if (!(await this.settled()) || this._locked) return;
		try {
			localStorage.setItem(this.lock_key(), frappe.csrf_token || "1"); // a reload keeps it locked
		} catch (e) {
			// private window: locked until a reload
		}
		this.show_lock();
	}

	show_lock() {
		if (this._locked) return;
		this._locked = true;
		this._lock_focus = document.activeElement;
		$(".resto-lock-hold").remove(); // the desk's cover while the floor loaded
		this.flush_taps();
		const f = this.floor || {};
		const name = f.user_name || frappe.session.user_fullname || frappe.session.user;
		let pin = "";
		let busy = false;
		let last_key = 0;
		let burst = false;
		let stamps = []; // when each digit came in
		const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "clear", "0", "back"];
		const $l = $(`<div class="rf-root rf-lock" role="dialog" aria-modal="true" aria-label="${__("The screen is locked")}">
			<div class="rf-lockbox">
				<div class="rf-lockico">${ICON.lock}</div>
				<b class="rf-lock-brand">${esc(f.brand || "")}</b>
				<div class="rf-lock-user"><span class="rf-av">${esc((name || "?").trim().charAt(0))}</span><b>${esc(name)}</b></div>
				<p class="rf-muted">${__("The screen is locked. Type your PIN to open it.")}</p>
				<div class="rf-dots" dir="ltr"></div>
				<p class="rf-lock-msg"></p>
				<div class="rf-lockkeys">${keys
					.map((k) => `<button class="rf-k" data-k="${k}">${k === "back" ? "⌫" : k === "clear" ? __("Clear") : k}</button>`)
					.join("")}</div>
				<button class="rf-btn rf-btn-p rf-lg rf-lock-open" data-l="open">${__("Open", null, "Resto Lock")}</button>
				<button class="rf-btn rf-lock-sw" data-l="switch">${__("Switch User")}</button>
			</div></div>`).appendTo(document.body);
		// Everything else on the page is out of reach (clicks, focus, screen readers, find in page).
		$(document.body)
			.children()
			.not($l)
			.each((_, el) => {
				if (!el.inert) {
					el.inert = true;
					el.setAttribute("data-rf-inert", "1");
				}
			});
		const draw = () => {
			$l.find(".rf-dots").html(Array.from({ length: Math.max(pin.length, 4) }, (_, i) => `<i class="${i < pin.length ? "on" : ""}"></i>`).join(""));
			$l.find(".rf-lock-open").prop("disabled", busy || pin.length < 4);
			$l.find(".rf-k, .rf-lock-sw").prop("disabled", busy);
		};
		const say = (msg) => $l.find(".rf-lock-msg").text(msg || "");
		const leave = async (forced) => {
			busy = true;
			draw();
			// Signed out on the server already (a locked PIN): the changes kept stay on this device for
			// this user's next sign-in, never sent without a session.
			const gone = await this.sign_out(say, forced);
			if (!gone) {
				busy = false;
				draw();
			}
		};
		const open = async () => {
			if (busy || pin.length < 4) return;
			stamps = [];
			busy = true;
			draw();
			say(__("Checking…"));
			let r;
			try {
				r = await Promise.race([
					frappe.xcall("resto.resto.approval.unlock", { pin }),
					new Promise((_, no) => setTimeout(() => no({ timeout: true }), 15000)),
				]);
			} catch (e) {
				busy = false;
				pin = "";
				draw();
				return say(
					navigator.onLine === false || (e && e.timeout)
						? __("The PIN is checked on the server: try again when connected.")
						: __("The PIN could not be checked. Try again, or Switch User.")
				);
			}
			if (!r) {
				busy = false;
				pin = "";
				draw();
				return say(__("The PIN could not be checked. Try again, or Switch User."));
			}
			if (r.ok) return this.unlock_done();
			pin = "";
			if (r.logout) {
				say(r.message);
				return setTimeout(() => leave(true), 1500);
			}
			busy = false;
			draw();
			say(r.message);
		};
		const key = (k) => {
			if (busy) return;
			if (k === "back") {
				pin = pin.slice(0, -1);
				stamps.pop();
			} else if (k === "clear") {
				pin = "";
				stamps = [];
			} else if (/^[0-9]$/.test(k) && pin.length < 8) {
				pin += k;
				stamps.push(Date.now());
			} else return;
			say("");
			draw();
		};
		$l.on("click", "[data-k]", (e) => key($(e.currentTarget).attr("data-k")));
		$l.on("click", "[data-l=open]", open);
		$l.on("click", "[data-l=switch]", () => !busy && leave(false));
		// The keyboard is the lock's while it is shown: nothing reaches the page or a window under it.
		// Caught first, on the window, wherever the focus is. A barcode scan (keys a few ms apart) is
		// not a PIN: it is dropped, and its Enter never spends a try.
		this._lock_capture = (e) => {
			if (!this._locked) return;
			e.stopPropagation();
			e.preventDefault();
			const now = Date.now();
			if (now - last_key > 300) burst = false; // a pause: a person again (a scan with no Enter)
			else if (now - last_key < 30 && !burst) {
				burst = true;
				pin = ""; // the scan's first key came in as a digit
				stamps = [];
				say("");
				draw();
			}
			last_key = now;
			const k = String(e.key || "")
				.replace(/[٠-٩]/g, (c) => String(c.charCodeAt(0) - 0x660))
				.replace(/[۰-۹]/g, (c) => String(c.charCodeAt(0) - 0x6f0));
			if (burst) {
				if (k === "Enter") {
					burst = false;
					pin = "";
					draw();
				}
				return;
			}
			// A slower scanner: a whole entry under 50 ms a key is not typed by a person.
			if (k === "Enter" && stamps.length >= 4 && (stamps[stamps.length - 1] - stamps[0]) / (stamps.length - 1) < 50) key("clear");
			else if (k === "Enter") open();
			else if (k === "Backspace") key("back");
			else if (k === "Escape") key("clear");
			else key(k);
		};
		window.addEventListener("keydown", this._lock_capture, true);
		draw();
		$l.find(".rf-lockbox").attr("tabindex", -1).trigger("focus");
	}

	unlock_done() {
		this._locked = false;
		this._last_act = Date.now();
		try {
			localStorage.removeItem(this.lock_key());
		} catch (e) {
			// nothing kept
		}
		if (this._lock_capture) window.removeEventListener("keydown", this._lock_capture, true);
		this._lock_capture = null;
		$("[data-rf-inert]").each((_, el) => {
			el.inert = false;
			el.removeAttribute("data-rf-inert");
		});
		$(".rf-lock, .resto-lock-hold").remove();
		// Back where the cashier was (the payment keypad, the menu search, a scanner's field).
		const back = this._lock_focus;
		this._lock_focus = null;
		if (back && document.contains(back) && back.focus) back.focus();
	}

	// Signs out (Switch User, Log out, a locked PIN). The session ends on the server first, and the
	// browser then leaves for the login page; until then nothing of the floor is open. Changes not sent
	// yet go first (the kitchen and the bill never miss them); while they cannot be sent it waits.
	// forced: the server ended the session already, so nothing more is sent. true: it signed out.
	// A failure keeps everything as it was, with a message.
	async sign_out(say, forced) {
		if (this._signing_out) return false;
		this._signing_out = true;
		try {
			if (!forced) {
				await this.flush_taps();
				await this.drain();
				const n = outbox().length;
				if (n) {
					say(__("{0} change(s) are not sent yet: sign out when the connection is back.", [n]));
					return false;
				}
				// Until the server answers, the session is alive: the curtain stays (a login page
				// opened now would send the browser back into the floor, unlocked).
				try {
					await Promise.race([frappe.xcall("logout"), new Promise((_, no) => setTimeout(() => no({ timeout: true }), 15000))]);
				} catch (e) {
					say(
						navigator.onLine === false || (e && e.timeout)
							? __("Not connected: try again when the connection is back.")
							: __("Could not sign out. Try again.")
					);
					return false;
				}
			}
			try {
				localStorage.setItem(this.lock_key(), "out"); // the other tabs go to the login page too
			} catch (e) {
				// this tab only
			}
			window.location.href = "/login";
			return true;
		} finally {
			this._signing_out = false;
		}
	}

	async print_receipt(sales_invoice, mode) {
		mode = mode || this.receipt_mode;
		if (mode === "server") {
			await frappe.xcall("resto.resto.printing.reprint", { doctype: "Sales Invoice", name: sales_invoice });
			return frappe.show_alert({ message: __("Sent to printer"), indicator: "green" });
		}
		frappe.show_alert({ message: __("Printing…"), indicator: "blue" });
		const pages = await frappe.xcall("resto.resto.printing.receipt_pages", { sales_invoice });
		if (await resto.print_pages(pages)) {
			await frappe.xcall("resto.resto.printing.receipt_printed", { sales_invoice });
		}
	}

	// ------------------------------------------------------------------ invoices (D-215)

	// The invoice history (D-212's lists): the open shift, today, the bills still owed and today's
	// returns; searched by invoice or order number, customer or phone; one invoice beside the list
	// with its lines, payments and change, to print again. Reading only.
	async invoices() {
		this._inv = { tab: "today", txt: "", order_type: "", mode: "", rows: [], more: false, sel: null, seq: 0 };
		$(this.page.body).find(".resto-floor").hide();
		this.$order.css("display", "flex").html(`
			<header class="rf-head rf-ohead">
				<button class="rf-btn rf-back">${ICON.back}<span>${__("Floor", null, "Resto")}</span></button>
				<div class="rf-otitle"><b>${__("Invoices", null, "Resto")}</b></div>
				<span class="rf-spacer"></span>
				<button class="rf-btn rf-here"></button>
			</header>
			<div class="rf-inv">
				<section class="rf-inv-l">
					<div class="rf-tabs rf-inv-tabs"></div>
					<div class="rf-inv-kpi"></div>
					<label class="rf-sbox">${ICON.search}<input class="rf-inv-q" type="search" autocomplete="off"
						placeholder="${__("Search by invoice or order number, customer or phone")}"></label>
					<div class="rf-chips rf-inv-types"></div>
					<div class="rf-chips rf-inv-modes"></div>
					<div class="rf-inv-list"></div>
				</section>
				<section class="rf-inv-d"><p class="rf-empty">${__("Choose an invoice to see it")}</p></section>
			</div>`);
		this.$order.find(".rf-back").on("click", () => this.close_order());
		this.draw_here();
		let timer;
		this.$order.find(".rf-inv-q").on("input", (e) => {
			clearTimeout(timer);
			timer = setTimeout(() => {
				this._inv.txt = $(e.currentTarget).val().trim();
				this.load_invoices();
			}, 350);
		});
		this.$order.on("click.rfinv", "[data-it]", (e) => {
			this._inv.tab = $(e.currentTarget).attr("data-it");
			this.draw_inv_tabs();
			this.load_invoices();
		});
		this.$order.on("click.rfinv", "[data-ot]", (e) => {
			this._inv.order_type = $(e.currentTarget).attr("data-ot");
			this.draw_inv_chips();
			this.load_invoices();
		});
		this.$order.on("click.rfinv", "[data-im]", (e) => {
			this._inv.mode = $(e.currentTarget).attr("data-im");
			this.draw_inv_chips();
			this.load_invoices();
		});
		this.$order.on("click.rfinv", "[data-inv]", (e) => this.show_invoice($(e.currentTarget).attr("data-inv")));
		this.$order.on("click.rfinv", ".rf-inv-more", () => this.load_invoices(true));
		const [counts, live] = await Promise.all([
			frappe.xcall("resto.resto.history.counts").catch(() => ({})),
			frappe.xcall("resto.resto.history.live").catch(() => null),
		]);
		if (!this._inv) return; // left meanwhile
		this._inv.counts = counts || {};
		if (this._inv.counts.shift != null) this._inv.tab = "shift";
		this.draw_inv_tabs();
		this.draw_inv_chips();
		if (live && live.net_sales != null) {
			const n = cint(live.sales_count);
			this.$order.find(".rf-inv-kpi").html(`
				<div class="rf-kpi"><span>${__("Shift Sales")}</span><b>${money(live.net_sales)}</b></div>
				<div class="rf-kpi"><span>${__("Average Bill")}</span><b>${money(n ? flt(live.net_sales) / n : 0)}</b></div>`);
		}
		this.load_invoices();
	}

	draw_here() {
		const here = this.prints_here();
		this.$order
			.find(".rf-here")
			.toggleClass("on", here)
			.html(`${ICON.print}<span>${__("This device prints bills and receipts")}</span>`)
			.attr("aria-pressed", here ? "true" : "false")
			.off("click")
			.on("click", () => {
				try {
					localStorage.setItem("resto_prints_here", here ? "0" : "1");
				} catch (e) {
					// private window: the default stays
				}
				this.draw_here();
			});
	}

	draw_inv_tabs() {
		const c = this._inv.counts || {};
		const tabs = [
			c.shift != null ? ["shift", __("Current Shift"), c.shift] : null,
			["today", __("Today", null, "Resto"), c.today],
			["owed", __("Unpaid Debts"), c.owed],
			["returns", __("Returns"), c.returns],
		].filter(Boolean);
		this.$order.find(".rf-inv-tabs").html(
			tabs
				.map(
					([k, label, n]) =>
						`<button class="rf-tab ${k === this._inv.tab ? "on" : ""}" data-it="${k}">${label}${n != null ? ` <span class="rf-cnt">${cint(n)}</span>` : ""}</button>`
				)
				.join("")
		);
	}

	draw_inv_chips() {
		const chip = (attr, val, label, on) => `<button class="rf-chip ${on ? "on" : ""}" ${attr}="${esc(val)}">${label}</button>`;
		const types = [["", __("All", null, "Resto")], ["Dine In", __("Dine In")], ["Takeaway", __("Takeaway", null, "Resto")], ["Delivery", __("Delivery", null, "Resto")]];
		this.$order.find(".rf-inv-types").html(types.map(([v, l]) => chip("data-ot", v, l, this._inv.order_type === v)).join(""));
		const modes = (this._inv.counts || {}).modes || [];
		this.$order
			.find(".rf-inv-modes")
			.html(modes.length > 1 ? modes.map((m) => chip("data-im", this._inv.mode === m ? "" : m, esc(__(m)), this._inv.mode === m)).join("") : "");
	}

	async load_invoices(more) {
		const s = this._inv;
		if (!s) return;
		const seq = ++s.seq;
		const args = { txt: s.txt, order_type: s.order_type, mode: s.mode, start: more ? s.rows.length : 0 };
		if (!s.txt) {
			if (s.tab === "shift") args.shift = "current";
			else if (s.tab === "today") args.period = "today";
			else if (s.tab === "owed") Object.assign(args, { status: "owed", period: "any" });
			else if (s.tab === "returns") Object.assign(args, { status: "returns", period: "today" });
		}
		const $list = this.$order.find(".rf-inv-list");
		if (!more) $list.html(`<p class="rf-empty">${__("Loading…")}</p>`);
		let r;
		try {
			r = await frappe.xcall("resto.resto.history.invoices", args);
		} catch (e) {
			if (seq === s.seq && !more) $list.html(`<p class="rf-empty">${__("Offline", null, "Resto")}</p>`);
			return;
		}
		if (this._inv !== s || seq !== s.seq) return; // a newer search, or the screen left
		s.rows = more ? s.rows.concat(r.invoices) : r.invoices;
		s.more = r.more;
		$list.html(
			s.rows.length
				? s.rows.map((v) => this.inv_row(v)).join("") +
						(s.more ? `<button class="rf-btn rf-inv-more">${__("Load More")}</button>` : "")
				: `<p class="rf-empty">${__("No invoices")}</p>`
		);
	}

	inv_status(v) {
		if (cint(v.is_return)) return `<span class="rf-st k-void">${__("Return")}</span>`;
		if (cint(v.overdue)) return `<span class="rf-st k-void">${__("Overdue", null, "Resto Invoice")}</span>`;
		const owed = flt(v.outstanding_amount);
		if (owed <= 0) return `<span class="rf-st k-ok">${__("Paid", null, "Resto Invoice")}</span>`;
		if (owed >= flt(v.total) - 0.005) return `<span class="rf-st k-void">${__("Unpaid", null, "Resto Invoice")}</span>`;
		return `<span class="rf-st k-warn">${__("{0} left", [money(owed)])}</span>`;
	}

	inv_where(v) {
		if (cint(v.is_return)) return __("Return of {0}", [esc(v.return_against)]);
		const where = v.resto_table
			? __("Table {0}", [esc(v.resto_table)], "Resto")
			: esc(__(v.resto_order_type || "", null, "Resto"));
		const who = v.customer_name && v.customer_name !== v.customer ? esc(v.customer_name) : esc(v.customer_name || "");
		return [where, v.resto_order ? esc(v.resto_order) : "", v.resto_table ? "" : who].filter(Boolean).join(" · ");
	}

	inv_row(v) {
		const modes = (v.modes || []).map((m) => esc(__(m))).join(" + ");
		return `<button class="rf-invr ${this._inv.sel === v.name ? "on" : ""}" data-inv="${esc(v.name)}">
			<div class="rf-orow1"><b>${esc(v.name)}</b><span class="rf-muted">${this.inv_where(v)}</span>
				<span class="rf-spacer"></span><b class="rf-amt">${money(v.total)}</b></div>
			<div class="rf-orow2"><span>${esc(hhmm(v.posting_time))}${v.posting_date !== frappe.datetime.get_today() ? ` · ${esc(frappe.datetime.str_to_user(v.posting_date))}` : ""}${modes ? ` · ${modes}` : ""} · ${esc(v.cashier || "")}</span>
				<span class="rf-spacer"></span>${this.inv_status(v)}</div>
		</button>`;
	}

	async show_invoice(name) {
		const s = this._inv;
		s.sel = name;
		this.$order.find(".rf-invr").removeClass("on").filter(`[data-inv="${CSS.escape(name)}"]`).addClass("on");
		const $d = this.$order.find(".rf-inv-d").html(`<p class="rf-empty">${__("Loading…")}</p>`);
		let v;
		try {
			v = await frappe.xcall("resto.resto.history.invoice", { name });
		} catch (e) {
			if (this._inv === s && s.sel === name) $d.html(`<p class="rf-empty">${__("Offline", null, "Resto")}</p>`);
			return;
		}
		if (this._inv !== s || s.sel !== name) return;
		const row = s.rows.find((r) => r.name === name) || {};
		const status = this.inv_status({ ...v, total: v.grand_total, overdue: row.overdue });
		const line = (label, value, cls) => `<div class="rf-trow ${cls || ""}"><span>${label}</span><span class="rf-spacer"></span><b class="rf-amt">${value}</b></div>`;
		$d.html(`
			<div class="rf-invd-h"><b class="rf-invd-n">${esc(v.name)}</b>${status}<span class="rf-spacer"></span>
				${cint(v.is_return) ? "" : `<button class="rf-btn" data-ia="print">${ICON.print}<span>${__("Print Again")}</span></button>`}
				${v.can_return ? `<button class="rf-btn" data-ia="return" title="${__("Opens the invoice: Create, then Return")}">${ICON.back}<span>${__("Return")}</span></button>` : ""}
			</div>
			<p class="rf-muted">${[
				this.inv_where({ ...v, resto_order_type: v.order_type }),
				__("Customer {0}", [esc(v.customer_name || v.customer)]),
				__("Cashier {0}", [esc(v.cashier)]),
				`${esc(frappe.datetime.str_to_user(v.posting_date))} ${esc(hhmm(v.posting_time))}`,
			].join(" · ")}</p>
			<table class="rf-invd-t"><thead><tr><th>${__("Item")}</th><th>${__("Qty")}</th><th>${__("Rate")}</th><th>${__("Amount")}</th></tr></thead>
				<tbody>${v.items
					.map((d) => `<tr><td>${esc(d.item_name)}</td><td>${fmt_qty(d.qty)}</td><td>${money(d.rate)}</td><td>${money(d.amount)}</td></tr>`)
					.join("")}</tbody></table>
			<div class="rf-tots">
				${line(__("Items Total"), money(v.total))}
				${flt(v.discount_amount) ? line(__("Discount"), `<span dir="ltr">−${money(v.discount_amount)}</span>`) : ""}
				${v.taxes.map((t) => line(esc(t.description), money(t.amount))).join("")}
				${line(__("Total", null, "Resto"), money(v.grand_total), "rf-tbig")}
			</div>
			<div class="rf-tots">
				${v.payments.map((p) => line(cint(v.is_return) ? esc(__(p.mode_of_payment)) : __("Paid ({0})", [esc(__(p.mode_of_payment))], "Resto"), money(p.amount))).join("")}
				${flt(v.change_amount) ? line(__("Change", null, "Resto"), money(v.change_amount)) : ""}
				${flt(v.outstanding_amount) > 0 ? line(__("Still Owed"), money(v.outstanding_amount), "rf-warn") : cint(v.is_return) ? "" : `<div class="rf-paidfull">${__("Paid in Full")} ✓</div>`}
			</div>
			<p class="rf-muted">${cint(v.printed) ? __("The receipt was printed") : __("The receipt was not printed yet")}</p>`);
		$d.find("[data-ia=print]").on("click", async (e) => {
			const btn = $(e.currentTarget).prop("disabled", true);
			try {
				await this.print_receipt(v.name, this.floor.receipt_mode || this.receipt_mode);
			} finally {
				btn.prop("disabled", false);
			}
		});
		$d.find("[data-ia=return]").on("click", () => frappe.set_route("Form", "Sales Invoice", v.name));
	}

	// ------------------------------------------------------------------ floor

	render_floor() {
		const f = this.floor;
		const halls = f.halls;
		if (!this.hall || !(this.hall === "__orders" || halls.find((h) => h.name === this.hall))) {
			this.hall = halls.length ? halls[0].name : "__orders";
		}
		this.render_head();
		const tab = (key, label, count) =>
			`<button class="rf-tab ${key === this.hall ? "on" : ""}" data-hall="${esc(key)}">${label} <span class="rf-cnt">${count}</span></button>`;
		const tabs = halls.map((h) => tab(h.name, esc(h.name), `${h.tables.filter((t) => t.status !== "free").length}/${h.tables.length}`));
		tabs.push(tab("__orders", __("Takeaway & Delivery"), f.orders.length));
		tabs.push(tab("__reservations", __("Reservations"), f.reservations_today || 0));
		this.put(this.$tabs, tabs.join(""), () =>
			this.$tabs.find("[data-hall]").on("click", (e) => {
				const key = $(e.currentTarget).attr("data-hall");
				if (key === "__reservations") return frappe.set_route("List", "Resto Reservation");
				this.hall = key;
				try {
					localStorage.setItem("resto_floor_hall", this.hall);
				} catch (err) {
					// remembered for this page only
				}
				this.render_floor();
			})
		);

		const r = f.roles;
		const takes = r.waiter || r.cashier || r.manager;
		this.put(this.$acts, `
			${r.cashier || r.manager ? `<button class="rf-btn" data-a="invoices">${ICON.receipt}<span>${__("Invoices", null, "Resto")}</span></button>
				<button class="rf-btn" data-a="shift">${ICON.drawer}<span>${__("Shift", null, "Resto")}</span></button>` : ""}
			${takes ? `<button class="rf-btn" data-a="delivery">${ICON.truck}<span>${__("New Delivery")}</span></button>
				<button class="rf-btn rf-btn-p" data-a="takeaway">${ICON.plus}<span>${__("New Takeaway")}</span></button>` : ""}`, () =>
			this.$acts.find("[data-a]").on("click", (e) => {
				const a = $(e.currentTarget).attr("data-a");
				const sh = this.floor.shift;
				if (a === "invoices") this.invoices();
				else if (a === "shift") this.shift_click(sh && sh.shift ? "menu" : "open");
				else if (a === "delivery") this.new_order("Delivery");
				else this.new_order("Takeaway");
			})
		);

		this.put(this.$alerts, this.kitchen_print_alert() + this.no_shift_banner(), () =>
			this.$alerts.find("[data-shift]").on("click", (e) => this.shift_click($(e.currentTarget).attr("data-shift")))
		);

		if (this.hall === "__orders") {
			this.$side.hide();
			return this.render_orders();
		}
		const hall = halls.find((h) => h.name === this.hall);
		this.$side.show();
		this.put(
			this.$side,
			`<div class="rf-side-h"><b>${__("Running Orders")}</b><span class="rf-cnt">${f.orders.length}</span></div>
			<div class="rf-side-l">${f.orders.map((o) => this.order_row(o)).join("") || `<p class="rf-empty">${__("No open takeaway or delivery orders.")}</p>`}</div>`,
			() => this.$side.find("[data-order]").on("click", (e) => this.load_order($(e.currentTarget).attr("data-order")))
		);
		if (!hall.tables.length) {
			this.put(this.$body, `<p class="rf-empty">${__("No tables in this hall yet.")}</p>`);
			return;
		}
		// The grid follows the page direction: in Arabic table 1 is on the right, and column 1 of a
		// placed table is the right-hand column (D-209).
		this.put(this.$body, `
			<div class="rf-grid ${f.can_order ? "" : "locked"}" style="grid-template-columns: repeat(${hall.grid_columns}, minmax(0, 1fr))">
				${hall.tables.map((t) => this.table_card(t)).join("")}</div>
			<div class="rf-legend">
				<span><i class="free"></i>${__("Free", null, "Resto")}</span>
				<span><i class="occupied"></i>${__("Occupied")}</span>
				<span><i class="bill"></i>${__("Bill Requested")}</span>
				<span><i class="paid"></i>${__("Paid, Waiting for the Food")}</span>
				<span><i class="reserved"></i>${__("Reserved")}</span>
			</div>`, () =>
			this.$body.find(".rf-tbl").on("click", (e) => {
				const name = $(e.currentTarget).attr("data-table");
				const now = this.floor.halls.flatMap((h) => h.tables).find((t) => t.name === name);
				if (now) this.open_table(now);
			})
		);
	}

	table_card(t) {
		const o = t.order;
		const pos = t.grid_row && t.grid_col ? `grid-row:${t.grid_row};grid-column:${t.grid_col};` : "";
		const label = { free: __("Free", null, "Resto"), occupied: __("Occupied"), bill: __("Bill Requested"), reserved: __("Reserved"), paid: __("Paid, Waiting for the Food") }[t.status];
		const seats = __("{0} seats", [t.seats || 0]);
		let body;
		if (o) {
			body = `<span class="rf-tsub">${o.guests ? __("{0} guests", [o.guests]) : seats} · ${__("{0} dishes", [fmt_qty(o.dishes)])}${o.waiter_name ? ` · ${esc(o.waiter_name)}` : ""}</span>
				${o.unsent_count ? `<span class="rf-unsent">${__("Not sent: {0}", [o.unsent_count])}</span>` : ""}
				<div class="rf-tfoot"><b class="rf-tamt">${money(o.bill_grand_total || o.net_total)}</b><span class="rf-spacer"></span>
					<span class="rf-tago">${__("for {0}", [dur(o.minutes)])}</span></div>
				${this.kitchen_progress(o)}`;
		} else if (t.paid) {
			body = `<span class="rf-tsub">${esc(t.paid.name)}</span>
				<span class="rf-tfoot rf-unsent">${__("{0} dish(es) not out of the kitchen yet", [t.paid.waiting])}</span>`;
		} else if (t.reservation) {
			body = `<span class="rf-tsub">${seats}</span>
				<span class="rf-tfoot rf-tres">${esc(t.reservation.customer_name)} · ${esc(hhmm(t.reservation.reservation_time))} · ${__("{0} guests", [t.reservation.guests])}</span>`;
		} else {
			body = `<span class="rf-tsub">${seats}</span><span class="rf-tfoot rf-thint">${__("Tap to open an order")}</span>`;
		}
		return `<button class="rf-tbl ${t.status} ${t.shape === "Round" ? "round" : ""}" data-table="${esc(t.name)}" style="${pos}">
			<div class="rf-ttop"><b class="rf-tname">${esc(t.name)}</b><span class="rf-spacer"></span><span class="rf-st">${label}</span></div>${body}</button>`;
	}

	// Where an order's sent dishes are (D-267), by dish count, in the colors of the order's own line
	// tags: at the kitchen, being made, ready. Served and voided dishes are not counted.
	kitchen_progress(o) {
		const parts = [
			["Sent", o.sent_qty, __("In the Kitchen", null, "Resto")],
			["InProgress", o.progress_qty, __("In Progress", null, "Resto")],
			["Ready", o.ready_qty, __("Ready")],
		].filter((p) => flt(p[1]) > 0);
		return parts.length
			? `<div class="rf-kprog">${parts.map(([slug, n, label]) => `<span class="rf-tag t-${slug}">${label} ${fmt_qty(n)}</span>`).join("")}</div>`
			: "";
	}

	// A takeaway or delivery order (and a dine-in order to bill again) in the lists.
	order_row(o) {
		const kind = o.rebill ? "rebill" : o.staff_meal ? "staff" : o.order_type === "Delivery" ? "delivery" : "takeaway";
		const who = o.staff_meal ? esc(o.staff_for || "") : [esc(o.customer_name || __("Walk-in", null, "Resto")), o.phone ? `<bdi dir="ltr">${esc(o.phone)}</bdi>` : ""].filter(Boolean).join(" · ");
		return `<button class="rf-orow ${o.status === "Bill Requested" ? "bill" : ""}" data-order="${esc(o.name)}">
			<div class="rf-orow1"><span class="rf-st k-${kind}">${o.staff_meal ? __("Staff Meal", null, "Resto") : __(o.order_type, null, "Resto")}</span><b>${o.order_no ? __("Order {0}", [o.order_no], "Resto") : esc(o.name)}</b>${o.order_no ? `<small class="rf-muted">${esc(o.name)}</small>` : ""}
				<span class="rf-spacer"></span>${o.staff_meal ? "" : `<b>${money(o.bill_grand_total || o.net_total)}</b>`}</div>
			<div class="rf-orow2"><span>${who}</span>
				${o.delivery_status ? `<span>· ${__(o.delivery_status, null, "Resto")}</span>` : ""}
				${o.status === "Bill Requested" ? `<span class="rf-warn">· ${__("Bill Requested")}</span>` : ""}
				${o.unsent_count ? `<span class="rf-unsent">${__("Not sent: {0}", [o.unsent_count])}</span>` : ""}
				<span class="rf-spacer"></span><span>${__("for {0}", [dur(o.minutes)])}</span></div>
			${this.kitchen_progress(o)}
			${o.rebill ? `<div class="rf-warn">${__("To Bill Again")} · ${__("Table {0}", [esc(o.resto_table)], "Resto")}</div>` : ""}
		</button>`;
	}

	render_orders() {
		const orders = this.floor.orders;
		if (!orders.length) {
			this.put(this.$body, `<p class="rf-empty">${__("No open takeaway or delivery orders.")}</p>`);
			return;
		}
		this.put(this.$body, `<div class="rf-olist">${orders.map((o) => this.order_row(o)).join("")}</div>`, () =>
			this.$body.find("[data-order]").on("click", (e) => this.load_order($(e.currentTarget).attr("data-order")))
		);
	}

	open_table(t) {
		if (t.order) return this.load_order(t.order.name);
		if (t.paid) return this.paid_table(t);
		if (!this.floor.can_order) return frappe.msgprint(esc(this.floor.no_order_reason));
		if (t.reservation) {
			frappe.confirm(
				__("Seat the reservation of {0} on this table?", [esc(t.reservation.customer_name)]),
				async () => {
					const name = await call("seat_reservation", { reservation: t.reservation.name, table: t.name }, true);
					this.load_order(name);
				},
				() => this.ask_guests(t)
			);
			return;
		}
		this.ask_guests(t);
	}

	// A table paid while its food is still in the kitchen (D-220): it frees by itself once every dish
	// is ready; the cashier may free it now, or seat new guests anyway.
	paid_table(t) {
		const r = this.floor.roles;
		const d = new frappe.ui.Dialog({
			title: `${__("Table {0}", [esc(t.name)], "Resto")} · ${__("Paid, Waiting for the Food")}`,
			fields: [
				{
					fieldname: "html",
					fieldtype: "HTML",
					options: `<p>${__("Order {0} is paid, and {1} dish(es) are not out of the kitchen yet. The table frees by itself when they are ready.", [esc(t.paid.name), t.paid.waiting])}</p>`,
				},
			],
		});
		if (r.cashier || r.host || r.manager) {
			d.set_primary_action(__("Free the Table"), async () => {
				d.hide();
				await call("free_table", { order: t.paid.name }, true);
				this.refresh();
			});
		}
		if (this.floor.can_order && (r.waiter || r.cashier || r.manager || r.host)) {
			d.set_secondary_action_label(__("Seat New Guests"));
			d.set_secondary_action(() => {
				d.hide();
				this.ask_guests(t);
			});
		}
		d.show();
	}

	ask_guests(t) {
		const d = new frappe.ui.Dialog({
			title: __("Open Table {0}", [esc(t.name)]),
			fields: [{ fieldname: "guests", fieldtype: "Int", label: __("Guests"), default: 2, reqd: 1 }],
			primary_action_label: __("Open", null, "Resto"),
			primary_action: async (v) => {
				d.hide();
				const name = await call("create_order", { order_type: "Dine In", table: t.name, guests: v.guests }, true);
				this.load_order(name);
			},
		});
		d.show();
	}

	// ------------------------------------------------------------------ customer

	// Phone first: what is typed in the phone or the name finds customers; one is picked with a click
	// (its phone, and on a delivery its address, fill in). Editing the phone or the name after picking
	// lets the customer go. The cashier can save a new customer once a phone is typed.
	customer_fields(o, with_address) {
		return [
			{ fieldname: "phone", fieldtype: "Data", label: __("Phone"), default: o.phone || "" },
			{ fieldname: "customer_name", fieldtype: "Data", label: __("Customer Name"), default: o.customer_name || "" },
			{ fieldname: "matches", fieldtype: "HTML" },
			{ fieldname: "address", fieldtype: "Small Text", label: __("Address"), default: o.address || "", hidden: with_address ? 0 : 1 },
			{ fieldname: "new_customer", fieldtype: "Check", label: __("Save as a new customer"), default: 0, hidden: 1 },
		];
	}

	bind_customer(d, o, tick_new) {
		const cashier = !!(this.floor && this.floor.roles.cashier);
		const $m = d.fields_dict.matches.$wrapper;
		const st = { picked: o.customer ? { name: o.customer, customer_name: o.customer_name, mobile_no: o.phone } : null, filled: {}, seq: 0 };
		d.resto_customer = st;
		let found = [];
		const digits = (v) => (v || "").replace(/[^0-9٠-٩۰-۹]/g, "");
		const show = () => {
			const p = st.picked;
			// A new customer is offered to the cashier once a phone is typed and no one is picked.
			const offer = cashier && !p && digits(d.get_value("phone")).length >= 7;
			d.set_df_property("new_customer", "hidden", offer ? 0 : 1);
			if (offer && tick_new && !st.offered) {
				st.offered = true;
				d.set_value("new_customer", 1);
			}
			if (p) {
				$m.html(`<div class="rf-cust-picked">✓ ${esc(p.customer_name || p.name)} ${esc(p.mobile_no || "")}
					<button class="btn btn-xs btn-default" data-unpick>${__("Change Customer")}</button></div>`);
				$m.find("[data-unpick]").on("click", () => unpick(true));
				return;
			}
			$m.html(
				found
					.map(
						(c, i) => `<button class="btn btn-default btn-sm rf-cust" data-i="${i}">
							<b>${esc(c.customer_name)}</b> ${esc(c.mobile_no || "")}
							${c.resto_address ? `<span class="text-muted">· ${esc(c.resto_address)}</span>` : ""}</button>`
					)
					.join("")
			);
			$m.find("[data-i]").on("click", (e) => pick(found[+$(e.currentTarget).attr("data-i")]));
		};
		const pick = async (c) => {
			st.picked = c;
			st.filled = {};
			found = [];
			const fill = async (field, value) => {
				if (!value) return;
				st.filled[field] = value;
				await d.set_value(field, value);
			};
			await fill("customer_name", c.customer_name);
			await fill("phone", c.mobile_no);
			if (!d.fields_dict.address.df.hidden && !d.get_value("address")) await fill("address", c.resto_address);
			await d.set_value("new_customer", 0);
			show();
		};
		// clear: what the picked customer filled in goes too (the button), or only the link (typing).
		const unpick = async (clear) => {
			st.picked = null;
			if (clear) {
				for (const [field, value] of Object.entries(st.filled)) {
					if (d.get_value(field) === value) await d.set_value(field, "");
				}
			}
			st.filled = {};
			show();
		};
		let t;
		const search = (txt) => {
			clearTimeout(t);
			const seq = ++st.seq;
			txt = (txt || "").trim();
			if (st.picked || txt.length < 2) {
				found = [];
				return show();
			}
			t = setTimeout(async () => {
				const rows = (await call("search_customers", { txt })) || [];
				if (seq !== st.seq) return; // an older answer, after a newer search
				found = rows;
				show();
			}, 300);
		};
		["phone", "customer_name"].forEach((f) =>
			d.fields_dict[f].$input.on("input", (e) => {
				const v = $(e.currentTarget).val();
				const p = st.picked;
				if (p && v !== (f === "phone" ? p.mobile_no : p.customer_name)) unpick(false);
				search(v);
			})
		);
		show();
	}

	customer_values(d, v) {
		// The picked customer, or none; a new one needs a name and a phone (a name alone stays on the order).
		const out = { ...v };
		delete out.matches;
		out.customer = d.resto_customer.picked ? d.resto_customer.picked.name : "";
		out.new_customer = !out.customer && out.new_customer && (out.customer_name || "").trim() && (out.phone || "").trim() ? 1 : 0;
		return out;
	}

	new_order(order_type) {
		if (!this.floor.can_order) return frappe.msgprint(esc(this.floor.no_order_reason));
		const d = new frappe.ui.Dialog({
			title: __("New {0} Order", [__(order_type, null, "Resto")]),
			fields: this.customer_fields({}, order_type === "Delivery"),
			primary_action_label: __("Start", null, "Resto"),
			primary_action: async (v) => {
				// Hidden once it worked: what was typed stays if the server refuses.
				const name = await call("create_order", { order_type, ...this.customer_values(d, v) }, true);
				d.hide();
				this.load_order(name);
			},
		});
		d.show();
		this.bind_customer(d, {}, true);
		d.fields_dict.phone.$input.focus();
	}

	// A staff meal (D-231): made like an order, then sent to the kitchen and taken from stock at cost
	// in one step (Send and Post), with no bill.
	new_staff_meal() {
		if (!(this.floor.roles.cashier || this.floor.roles.manager)) return;
		const d = new frappe.ui.Dialog({
			title: __("Staff Meal", null, "Resto"),
			fields: [
				{ fieldname: "note", fieldtype: "HTML", options: `<p class="text-muted">${__("Taken from the kitchen's stock at cost, with no bill. Add the dishes (and their sides) as for any order, and free ingredients for what is not on the menu.")}</p>` },
				{ fieldname: "staff_for", fieldtype: "Data", label: __("For Whom") },
			],
			primary_action_label: __("Start", null, "Resto"),
			primary_action: async (v) => {
				const name = await frappe.xcall("resto.resto.staff_meal.create", { staff_for: v.staff_for || "" });
				d.hide();
				this.load_order(name);
			},
		});
		d.show();
	}

	// A free ingredient of a staff meal: any stock item, typed in a recipe unit (grams for an item
	// stocked by the kilo, millilitres by the litre, D-250) or another unit it converts to; kept in
	// its stock unit. An unusual quantity needs a manager's PIN.
	staff_item() {
		const o = this.order;
		let info = null;
		const factor = () => {
			const u = info && info.units.find((x) => x.uom === d.get_value("uom"));
			return u ? flt(u.factor) : 0;
		};
		const in_stock = () => flt(flt(d.get_value("qty")) * factor(), info ? info.precision : 3);
		const hint = () => {
			const same = !info || d.get_value("uom") === info.stock_uom;
			d.set_df_property("qty", "description", same || !flt(d.get_value("qty")) ? "" : `= ${fmt_stock_qty(in_stock())} ${esc(__(info.stock_uom))}`);
		};
		const d = new frappe.ui.Dialog({
			title: __("Free Ingredient"),
			fields: [
				{
					fieldname: "item_code",
					fieldtype: "Link",
					options: "Item",
					label: __("Item"),
					reqd: 1,
					get_query: () => ({ filters: { is_stock_item: 1, disabled: 0, has_variants: 0 } }),
					onchange: () => {
						const code = d.get_value("item_code");
						info = null;
						d.set_df_property("uom", "options", "");
						d.set_value("uom", "");
						hint();
						if (!code) return;
						frappe.xcall("resto.resto.units.item_units", { item_code: code }).then((r) => {
							if (code !== d.get_value("item_code")) return;
							info = r;
							d.set_df_property("uom", "options", r.units.map((u) => ({ value: u.uom, label: __(u.uom) })));
							d.set_value("uom", r.default);
							hint();
						});
					},
				},
				{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), reqd: 1, onchange: () => hint() },
				{ fieldname: "uom", fieldtype: "Select", label: __("Unit"), reqd: 1, options: "", onchange: () => hint() },
			],
			primary_action_label: __("Add", null, "Resto"),
			primary_action: async (v) => {
				if (!info) return;
				const send = (ok) =>
					new Promise((resolve, reject) =>
						frappe.call({
							method: "resto.resto.staff_meal.set_item",
							args: { order: o.name, item_code: v.item_code, qty: v.qty, uom: v.uom, ...ok },
							callback: (r) => resolve(r.message),
							error: (r) => reject(r || {}),
						})
					);
				let done = null;
				try {
					done = await send({});
				} catch (r) {
					if (r.exc_type !== "ApprovalRequired" || this.floor.roles.manager) return;
					const ok = await ask_approval(this.floor, __("Staff meal: {0} {1} of {2}", [fmt_stock_qty(in_stock()), __(info.stock_uom), v.item_code]));
					if (!ok) return;
					done = await send(ok).catch(() => null);
				}
				if (!done) return;
				d.hide();
				this.load_order(o.name);
			},
		});
		d.show();
	}

	async remove_staff_item(item_code) {
		const o = this.order;
		await frappe.xcall("resto.resto.staff_meal.set_item", { order: o.name, item_code, qty: 0 });
		this.load_order(o.name);
	}

	async post_staff_meal() {
		const o = this.order;
		const dishes = o.items.filter((d) => d.kitchen_status !== "Void").reduce((s, d) => s + flt(d.qty), 0);
		const free = (o.staff_items || []).length;
		if (!dishes && !free) return frappe.msgprint(__("Add what the staff eat first"));
		const sure = await confirm_post([
			`<p>${__("This staff meal goes to the kitchen now, and what it is made of is taken from the kitchen's stock at cost, with no bill.")}</p>`,
			`<p><b>${__("{0} dishes, {1} free ingredients", [fmt_qty(dishes), free])}</b>${o.staff_for ? ` · ${__("For {0}", [esc(o.staff_for)])}` : ""}</p>`,
		]);
		if (!sure) return;
		const r = await frappe.xcall("resto.resto.staff_meal.post", { order: o.name });
		frappe.show_alert({ message: __("Staff meal posted at a cost of {0}", [money(r.cost)]), indicator: "green" }, 8);
		this.close_order();
	}

	// ------------------------------------------------------------------ shift (Resto Shift, D-203)

	// The shift as a pill in the header: open since when, or none (D-211).
	shift_pill() {
		const f = this.floor;
		const s = f.shift;
		if (s && s.shift && !s.other_profile) {
			return `<button class="rf-pill rf-pill-green" data-shift="menu"><i class="rf-dotc"></i><span class="rf-shift-t">${__("Shift open for {0}", [dur(minutes_since(s.since))])}</span></button>`;
		}
		if (!s) {
			// A waiter has no drawer: what matters is whether a cashier's shift is open.
			return f.can_order
				? `<span class="rf-pill rf-pill-green"><i class="rf-dotc"></i><span>${__("Shift open", null, "Resto")}</span></span>`
				: `<span class="rf-pill rf-pill-amber"><i class="rf-dotc"></i><span>${__("No open shift", null, "Resto")}</span></span>`;
		}
		return `<button class="rf-pill rf-pill-amber" data-shift="${s.other_profile ? "close" : "open"}"><i class="rf-dotc"></i><span>${s.other_profile ? __("Close Shift") : __("No open shift", null, "Resto")}</span></button>`;
	}

	// Orders wait for a shift (D-211): the floor says so, and a cashier opens one from here.
	no_shift_banner() {
		const f = this.floor;
		const s = f.shift;
		if (s && s.shift && s.other_profile) {
			return `<div class="rf-banner"><div><b>${__("The floor is closed until a shift is opened")}</b>
				<span>${__("Your open shift is on POS Profile {0}; close it and open one on {1}", [esc(s.other_profile), esc(s.pos_profile)])}</span></div>
				<button class="rf-btn" data-shift="close">${__("Close Shift")}</button></div>`;
		}
		if (f.can_order) {
			return s && !s.shift && !s.required ? `<div class="rf-banner soft"><span>${__("No shift open")}</span>
				<button class="rf-btn" data-shift="open">${__("Open Shift")}</button></div>` : "";
		}
		return `<div class="rf-banner"><div><b>${__("The floor is closed until a shift is opened")}</b><span>${esc(f.no_order_reason)}</span></div>
			${s ? `<button class="rf-btn rf-btn-p" data-shift="open">${ICON.drawer}<span>${__("Open Shift")}</span></button>` : ""}</div>`;
	}

	shift_click(act) {
		if (act === "open") return this.open_shift();
		if (act === "close") return this.close_shift();
		if (act === "expense") return this.drawer_expense();
		if (act === "purchase") return this.cash_purchase();
		if (act === "debt") return this.collect_debt();
		const s = this.floor.shift || {};
		if (!s.shift) return this.open_shift();
		this.menu_dialog(__("Shift", null, "Resto"), [
			[__("Staff Meal", null, "Resto"), () => this.new_staff_meal()],
			[__("Collect Debt"), () => this.collect_debt()],
			[__("Drawer Expense"), () => this.drawer_expense()],
			[__("Cash Purchase"), () => this.cash_purchase()],
			[__("Staff Advance from the Drawer"), () => this.staff_advance()],
			[__("Cash In to the Drawer"), () => this.cash_in()],
			[__("Cash Drop to the Main Safe"), () => this.cash_drop()],
			[__("Close Shift"), () => this.close_shift()],
		]);
	}

	// The user button's menu (D-214), in two columns (D-266): the drawer's money on one side, the
	// screens, the shift and the PINs on the other; logging out last, apart.
	user_menu() {
		const f = this.floor;
		const r = f.roles;
		const s = f.shift || {};
		const drawer = [];
		const views = [];
		if (r.cashier || r.manager) {
			views.push([__("Invoices", null, "Resto"), () => this.invoices()]);
			if (s.shift) {
				drawer.push(
					[__("Staff Meal", null, "Resto"), () => this.new_staff_meal()],
					[__("Collect Debt"), () => this.collect_debt()],
					[__("Drawer Expense"), () => this.drawer_expense()],
					[__("Cash Purchase"), () => this.cash_purchase()],
					[__("Staff Advance from the Drawer"), () => this.staff_advance()],
					[__("Cash In to the Drawer"), () => this.cash_in()]
				);
			}
			// Also right after the close: the counted cash taken to the safe (D-245).
			drawer.push([__("Cash Drop to the Main Safe"), () => this.cash_drop()]);
		}
		views.push([__("Kitchen Display"), () => frappe.set_route("resto-kitchen")]);
		if (r.cashier || r.manager) {
			// What the drawer and each method should hold now, without closing (D-262).
			if (s.shift) views.push([__("Shift Summary Now"), () => this.shift_summary()]);
			views.push(s.shift ? [__("Close Shift"), () => this.close_shift()] : [__("Open Shift"), () => this.open_shift()]);
		}
		// The lock screen only when Resto Settings turns it on (D-216); a manager's approval PIN always.
		if (f.screen_lock) views.push([`${ICON.lock}<span>${__("Lock Screen")}</span>`, () => this.lock()]);
		if (r.manager) views.push([__("My Approval PIN"), () => this.set_my_pin("approve")]);
		if (f.screen_lock) views.push([r.manager ? __("My Lock PIN") : __("My PIN"), () => this.set_my_pin("lock")]);
		const out = [
			__("Log out"),
			() =>
				frappe.confirm(__("Log out of this device?"), async () => {
					if (this.order && !(await this.settled())) return;
					this.sign_out((msg) => frappe.msgprint(msg));
				}),
			"rf-menu-out",
		];
		this.menu_dialog(`${esc(f.user_name)}${this.role_label() ? ` · ${esc(this.role_label())}` : ""}`, { cols: [drawer, views], foot: [out] });
	}

	// acts: a list of [label, action, class], or {cols: [list, list], foot: list} for a menu in two
	// columns (one on a phone) with its last buttons under both (D-266).
	menu_dialog(title, acts) {
		const grouped = !Array.isArray(acts);
		const cols = grouped ? acts.cols.filter((c) => c.length) : [acts];
		const all = [...cols.flat(), ...(grouped ? acts.foot || [] : [])];
		const two = cols.length > 1;
		const d = new frappe.ui.Dialog({ title, size: two ? "large" : undefined, fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		d.$wrapper.on("hidden.bs.modal", () => setTimeout(() => d.$wrapper.remove(), 0));
		let n = 0;
		const btns = (list) => list.map((a) => `<button class="rf-btn rf-menu-btn ${a[2] || ""}" data-i="${n++}">${a[0]}</button>`).join("");
		const body = cols.map((c) => `<div class="rf-menu-list">${btns(c)}</div>`).join("");
		const foot = grouped && (acts.foot || []).length ? `<div class="rf-menu-foot">${btns(acts.foot)}</div>` : "";
		d.fields_dict.html.$wrapper.html(`<div class="rf-root rf-mdlg">${two ? `<div class="rf-menu-cols">${body}</div>` : body}${foot}</div>`);
		acts = all;
		d.fields_dict.html.$wrapper.find("button").on("click", (e) => {
			d.hide();
			acts[$(e.currentTarget).attr("data-i")][1]();
		});
		d.show();
	}

	async open_shift() {
		const data = await frappe.xcall("resto.resto.shift.opening_data");
		if (data.open_shift) {
			frappe.msgprint(__("You already have an open shift: {0}", [esc(data.open_shift)]));
			return this.refresh();
		}
		// The profile, the cashier and now, then the opening balance of each method.
		const ro = (fieldname, label, value, fieldtype = "Data") => ({ fieldname, label, fieldtype, default: value, read_only: 1 });
		const d = new frappe.ui.Dialog({
			title: __("Open Shift"),
			fields: [
				ro("pos_profile", __("POS Profile"), data.pos_profile),
				ro("company", __("Company"), data.company),
				ro("warehouse", __("Warehouse"), data.warehouse),
				{ fieldtype: "Column Break" },
				ro("cashier", __("Cashier"), data.cashier),
				ro("now", __("Date and Time"), data.now, "Datetime"),
				ro("currency", __("Currency"), data.currency),
				{ fieldtype: "Section Break", label: __("Opening Balance (Optional)") },
				{ fieldtype: "HTML", options: `<p class="text-muted">${__("Count the money in the drawer.")}</p>` },
				...(data.modes.length
					? data.modes.map((m, i) => ({ fieldname: "m_" + i, fieldtype: "Currency", label: m, default: 0, options: "currency" }))
					: [{ fieldtype: "HTML", options: `<p class="text-danger">${__("No payment methods configured for this POS Profile")}</p>` }]),
			],
			primary_action_label: __("Open Shift"),
			primary_action: async (v) => {
				// The questions come before the wait: a screen frozen by shift_busy would sit above
				// their dialogs and nothing could be tapped. One opening at a time meanwhile.
				const $b = d.get_primary_btn();
				if ($b.prop("disabled")) return;
				$b.prop("disabled", true);
				const amounts = {};
				data.modes.forEach((m, i) => (amounts[m] = flt(v["m_" + i])));
				const args = { amounts: JSON.stringify(amounts) };
				try {
					// The count against the drawer's books (D-258): more needs a reason, less is told.
					const found = await frappe.xcall("resto.resto.opening_check.preview", { amounts: args.amounts });
					if (found.length) {
						const extra = await this.opening_choice(found);
						if (!extra) return;
						if (extra === "change") {
							const ok = await ask_approval(this.floor, __("Change from the main safe at the opening"));
							if (!ok) return;
							Object.assign(args, ok);
						}
						args.extra = extra;
					}
				} finally {
					$b.prop("disabled", false);
				}
				await this.shift_busy(d, async () => {
					await frappe.xcall("resto.resto.shift.open_shift", args);
					d.hide();
					frappe.show_alert({ message: __("Shift opened"), indicator: "green" });
					this.refresh();
				});
			},
		});
		d.show();
	}

	// The opening count is not what the drawer's books hold (D-258). More: change from the main
	// safe (posted now, a manager's PIN) or to review later; less: the shift opens and is marked.
	opening_choice(found) {
		return new Promise((resolve) => {
			const more = found.some((f) => f.more);
			let chosen = null;
			const lines = found.map((f) => `<p><b>${esc(__(f.mode_of_payment))}</b>: ${esc(f.message)}</p>`).join("");
			const note = `<p class="text-muted">${__("The manager is told of a shift opened to review or short.")}</p>`;
			const d = new frappe.ui.Dialog({
				title: __("The Drawer Does Not Match the Books"),
				fields: [{ fieldtype: "HTML", options: lines + note }],
				primary_action_label: more ? __("Change from the Main Safe") : __("Open Shift"),
				primary_action: () => {
					chosen = more ? "change" : "review";
					d.hide();
				},
				secondary_action_label: more ? __("Review Later") : __("Cancel"),
				secondary_action: () => {
					chosen = more ? "review" : null;
					d.hide();
				},
			});
			d.onhide = () => resolve(chosen);
			d.show();
		});
	}

	// One tap at a time: the button waits for the server, and the dialog (with what was typed) stays
	// open when the server refuses.
	async shift_busy(d, fn) {
		const $b = d.get_primary_btn();
		if ($b.prop("disabled")) return;
		$b.prop("disabled", true);
		frappe.dom.freeze(__("Please wait…"));
		try {
			await fn();
		} finally {
			frappe.dom.unfreeze();
			$b.prop("disabled", false);
		}
	}

	// The shift's figures: sales, returns, expenses, collected and
	// on account, taxes, the invoices, and per payment method opening, expected, counted and difference.
	// entry: the counted amounts are inputs (data-m); otherwise the closed shift's report.
	shift_report(r, entry) {
		const s = r.summary || {};
		const diff = (v) => {
			const n = flt(v);
			if (Math.abs(n) < 0.005) return `<span class="text-success">✓ ${__("Balanced")}</span>`;
			return `<span class="${n < 0 ? "text-danger" : "text-warning"}">${n < 0 ? __("Short {0}", [money(-n)]) : __("Over {0}", [money(n)])}</span>`;
		};
		const metric = (label, value, note) =>
			`<div class="rf-metric"><div class="text-muted">${label}</div><b>${value}</b>${note ? `<div class="text-muted small">${note}</div>` : ""}</div>`;
		const parts = [];
		const live = !entry && !!r.live;
		if (live) {
			parts.push(`<div class="alert alert-info"><b>${__("Shift open · as of {0}", [esc(frappe.datetime.str_to_user(r.as_of))])}</b></div>`);
		}
		// The header: the profile, when the shift started (and ended), and how long it ran.
		const start = r.period_start;
		const end = r.period_end || frappe.datetime.now_datetime();
		const mins = start ? Math.max(0, Math.round(moment(end).diff(moment(start), "minutes"))) : 0;
		parts.push(`<p class="text-muted">${r.pos_profile ? `${__("POS Profile")}: <b>${esc(r.pos_profile)}</b> · ` : ""}
			${start ? `${__("Shift start")}: ${esc(frappe.datetime.str_to_user(start))}` : ""}
			${r.period_end ? ` · ${__("Shift end")}: ${esc(frappe.datetime.str_to_user(r.period_end))}` : ""}
			${start ? ` · ${__("Duration")}: ${__("{0} h {1} min", [Math.floor(mins / 60), mins % 60])}` : ""}</p>`);
		if (entry && r.open_orders) {
			parts.push(`<div class="alert alert-warning">${__("{0} orders are still open: their bills are not in this shift.", [r.open_orders])}</div>`);
		} else if (live && r.open_orders) {
			parts.push(`<div class="alert alert-warning">${__("{0} orders are still open: not in these figures until they are paid.", [r.open_orders])}</div>`);
		}
		if (r.summary) {
			parts.push(`<div class="rf-metrics">
				${metric(__("Gross Sales"), money(s.gross_sales), __("{0} invoices · before returns", [s.sales_count || (r.invoices || []).filter((i) => !i.is_return).length]))}
				${s.returns_count ? metric(__("Returns"), "-" + money(s.returns_total), __("{0} returns", [s.returns_count])) : ""}
				${metric(__("Net Sales"), money(s.net_sales), __("After returns are taken off"))}
				${metric(__("Tax Collected"), money(s.tax_total), __("VAT on the shift's invoices"))}
				${metric(__("Collected"), money(s.collected), s.debts_count ? __("Received by every payment method, {0} of it debts collected", [money(s.debts_total)]) : __("Received by every payment method"))}
				${s.outstanding > 0.005 ? metric(__("On Account"), money(s.outstanding), __("Not collected")) : ""}
				${s.debts_count ? metric(__("Debts Collected"), money(s.debts_total), __("{0} payments", [s.debts_count])) : ""}
				${s.expenses_count ? metric(__("Drawer Running Expenses"), "-" + money(s.expenses_total), __("{0} expenses · transport, cleaning and the like, not into stock", [s.expenses_count])) : ""}
				${s.purchases_count ? metric(__("Drawer Stock Purchases"), "-" + money(s.purchases_total), __("{0} purchase invoices · into the kitchen's stock", [s.purchases_count])) : ""}
				${s.advances_count ? metric(__("Staff Advances"), "-" + money(s.advances_total), __("{0} advances · owed by the workers, not an expense", [s.advances_count])) : ""}
				${s.cash_in_count ? metric(__("Cash In to the Drawer"), "+" + money(s.cash_in_total), __("{0} times · advances paid back, change and the like, not sales", [s.cash_in_count])) : ""}
				${s.drops_count ? metric(__("Cash Drops to the Main Safe"), "-" + money(s.drops_total), __("{0} drops · moved to the safe, not spent", [s.drops_count])) : ""}
				${s.expenses_count || s.purchases_count || s.advances_count || s.cash_in_count || s.drops_count ? metric(__("Net In for the Shift"), money(s.net_cash), net_caption(s)) : ""}
				${s.staff_meals_count ? metric(__("Staff Meals"), money(s.staff_meals_cost), __("{0} meals · at stock cost, not cash", [s.staff_meals_count])) : ""}
			</div>`);
			const inv = (r.invoices || [])
				.map(
					(i) => `<tr><td>${esc(frappe.datetime.str_to_user(i.posting_date))}</td><td>${esc(i.name)}</td><td>${esc(i.customer || "")}</td>
						<td>${i.is_return ? __("Return") : __("Sale")}</td><td class="rf-amt">${money(i.grand_total)}</td>
						<td class="rf-amt">${i.outstanding_amount > 0.005 ? __("Unpaid: {0}", [money(i.outstanding_amount)]) : ""}</td></tr>`
				)
				.join("");
			parts.push(
				inv
					? `<details><summary><b>${__("Invoice Details")}</b> (${(r.invoices || []).length})</summary>
						<table class="table table-condensed"><tr><th>${__("Posting Date")}</th><th>${__("Invoice")}</th><th>${__("Customer")}</th><th>${__("Type")}</th><th>${__("Amount")}</th><th></th></tr>${inv}</table></details>`
					: `<p class="text-muted">${__("No Sales During This Shift")}</p>`
			);
			if ((r.expenses || []).length) {
				parts.push(`<details><summary><b>${__("Drawer Running Expenses")}</b></summary><table class="table table-condensed">
					${r.expenses.map((e) => `<tr><td>${esc(e.expense_account)}</td><td>${esc(e.remarks || "")}</td><td class="rf-amt">${money(e.amount)}</td></tr>`).join("")}</table></details>`);
			}
			if ((r.purchases || []).length) {
				parts.push(`<details><summary><b>${__("Drawer Stock Purchases")}</b></summary><table class="table table-condensed">
					${r.purchases
						.map((e) => `<tr><td>${esc(e.supplier_name || e.supplier || "")}</td><td>${esc(e.items_text || "")}</td><td class="rf-amt">${money(e.amount)}</td></tr>`)
						.join("")}</table></details>`);
			}
			if ((r.advances || []).length) {
				parts.push(`<details><summary><b>${__("Staff Advances")}</b></summary><table class="table table-condensed">
					${r.advances
						.map((a) => `<tr><td>${esc(a.employee_name || a.employee)}</td><td>${esc(a.remarks || "")}</td><td class="rf-amt">${money(a.amount)}</td></tr>`)
						.join("")}</table></details>`);
			}
			const opened = (r.rows || []).filter((x) => x.opening_check && x.opening_check !== "Matches");
			if (opened.length) {
				parts.push(`<details open><summary><b>${__("Opening Difference")}</b></summary><table class="table table-condensed">
					${opened
						.map(
							(x) => `<tr><td>${esc(__(x.mode_of_payment))}</td><td>${esc(__(x.opening_check))}${x.opening_entry ? ` · ${esc(x.opening_entry)}` : ""}</td>
								<td class="rf-amt">${x.opening_check === "Not Checked" ? "" : money(x.opening_difference)}</td></tr>`
						)
						.join("")}</table></details>`);
			}
			if ((r.cash_moves || []).length) {
				parts.push(`<details><summary><b>${__("Cash In and Drops")}</b></summary><table class="table table-condensed">
					${r.cash_moves
						.map(
							(m) => `<tr><td>${esc(__(m.reason, null, m.reason === "Other" ? "Resto" : undefined))}${m.employee_name ? `: ${esc(m.employee_name)}` : ""}</td><td>${esc(m.remarks || "")}</td>
								<td class="rf-amt">${m.move === "Cash In" ? "+" : "−"}${money(m.amount)}</td></tr>`
						)
						.join("")}</table></details>`);
			}
			if ((r.taxes || []).length) {
				parts.push(`<details><summary><b>${__("Tax Summary")}</b></summary><table class="table table-condensed">
					${r.taxes.map((t) => `<tr><td>${esc(t.account_head)}</td><td>${flt(t.rate)}%</td><td class="rf-amt">${money(t.amount)}</td></tr>`).join("")}</table></details>`);
			}
		}
		const blind = (entry || live) && !r.summary;
		if (live && blind) {
			parts.push(`<p class="text-muted">${__("Counting blind: the amounts each payment method should hold are shown to the manager only.")}</p>`);
			return parts.join("");
		}
		const list = r.rows || [];
		const rows = list
			.map((p, i) => {
				const counted = entry
					? `<input type="number" step="0.01" min="0" class="form-control input-sm rf-amt" data-m="${i}" value="">`
					: money(p.closing_amount);
				// The breakdown of the expected amount, only where the expected amount is shown.
				const name = blind
					? esc(p.mode_of_payment)
					: `<details class="rf-xparts"><summary>${esc(p.mode_of_payment)}</summary>${expected_parts(p)}</details>`;
				return `<tr><td>${name}</td>
					${blind ? "" : `<td class="rf-amt">${money(p.opening_amount)}</td><td class="rf-amt">${money(p.expected_amount)}</td>`}
					${live ? "" : `<td class="rf-amt">${counted}</td>`}
					${blind || live ? "" : `<td class="rf-amt" data-d="${i}">${entry ? "" : diff(p.difference)}</td>`}</tr>`;
			})
			.join("");
		// The totals of all methods (none while counting blind).
		const sum = (k) => list.reduce((a, p) => a + flt(p[k]), 0);
		const total = blind
			? ""
			: `<tr class="rf-total"><th>${__("Total", null, "Resto")}</th><th class="rf-amt">${money(sum("opening_amount"))}</th>
				<th class="rf-amt">${money(sum("expected_amount"))}</th>
				${live ? "" : `<th class="rf-amt" data-tc>${entry ? "" : money(sum("closing_amount"))}</th>
				<th class="rf-amt" data-td>${entry ? "" : diff(sum("difference"))}</th>`}</tr>`;
		parts.push(`<h5>${__("Payment Reconciliation")}</h5>
			${entry ? `<p class="text-muted">${__("Count the money in the drawer and enter what you counted.")}</p>` : ""}
			<table class="table table-condensed"><tr><th></th>
				${blind ? "" : `<th>${__("Opening")}</th><th>${__("Expected")}</th>`}${live ? "" : `<th>${__("Counted")}</th>`}${blind || live ? "" : `<th>${__("Difference")}</th>`}</tr>
			${rows}${total}</table>`);
		return parts.join("");
	}

	// Money paid out of the drawer: a Journal Entry from the payment method's account; the shift's
	// expected amount goes down by it.
	async drawer_expense() {
		const data = await frappe.xcall("resto.resto.shift.expense_accounts");
		if (!data.modes.length) return frappe.msgprint(__("This shift has no cash payment method"));
		// One id per dialog: a retried click after a lost answer gives the same entry.
		const request_id = frappe.utils.get_random(20);
		const d = new frappe.ui.Dialog({
			title: __("Drawer Expense"),
			fields: [
				{ fieldname: "mode_of_payment", fieldtype: "Select", label: __("Paid From"), options: data.modes.join("\n"), default: data.modes[0], reqd: 1 },
				{
					fieldname: "expense_account",
					fieldtype: "Select",
					label: __("Expense Account"),
					options: [{ value: "", label: "" }, ...data.accounts.map((a) => ({ value: a.name, label: a.account_name }))],
					reqd: 1,
					description: __("Food and other stock bought for cash is a Cash Purchase, so it enters the stock."),
				},
				{
					fieldname: "amount",
					fieldtype: "Currency",
					label: __("Amount"),
					reqd: 1,
					description: data.cap_left == null ? "" : __("Left of this shift's cap: {0}", [money(data.cap_left)]),
				},
				{ fieldname: "remarks", fieldtype: "Data", label: __("For What"), reqd: 1 },
				{ fieldname: "receipt", fieldtype: "Attach", label: __("Receipt"), reqd: data.receipt_required ? 1 : 0 },
			],
			primary_action_label: __("Save and Post"),
			primary_action: async (v) => {
				const account = (data.accounts.find((a) => a.name === v.expense_account) || {}).account_name || v.expense_account;
				const sure = await confirm_post([
					`<p>${__("{0} will be paid from the drawer «{1}» to {2}, for: {3}.", [
						`<b>${money(v.amount)}</b>`,
						esc(v.mode_of_payment),
						`<b>${esc(account)}</b>`,
						esc(v.remarks),
					])}</p>`,
				]);
				if (!sure) return;
				return this.shift_busy(d, async () => {
					const je = await frappe.xcall("resto.resto.shift.add_expense", { ...v, request_id });
					d.hide();
					frappe.show_alert({ message: __("Expense recorded: {0}", [je]), indicator: "green" });
				});
			},
		});
		d.show();
	}

	// Food or other stock bought for cash out of the drawer (D-225): a paid Purchase Invoice that
	// receives it into the kitchen at what was paid; the shift's expected cash goes down by it.
	async cash_purchase() {
		const data = await frappe.xcall("resto.resto.shift.purchase_data");
		if (!data.modes.length) return frappe.msgprint(__("This shift has no cash payment method"));
		// One id per dialog: a retried click after a lost answer gives the same purchase.
		const request_id = frappe.utils.get_random(20);
		const suppliers = [{ value: "", label: __("Cash Supplier", null, "Resto") }, ...data.suppliers.filter((x) => x.name !== data.cash_supplier).map((x) => ({ value: x.name, label: x.supplier_name || x.name }))];
		const total = (rows) => (rows || []).reduce((a, r) => a + flt(r.amount), 0);
		const d = new frappe.ui.Dialog({
			title: __("Cash Purchase"),
			fields: [
				{
					fieldname: "note",
					fieldtype: "HTML",
					options: `<p class="text-muted">${__("Received into {0} at what was paid, and paid from the drawer.", [esc(data.warehouse || "")])}</p>`,
				},
				{
					fieldname: "items",
					fieldtype: "Table",
					label: __("Items Bought"),
					cannot_add_rows: false,
					in_place_edit: true,
					reqd: 1,
					data: [],
					fields: [
						{
							fieldname: "item_code",
							fieldtype: "Link",
							options: "Item",
							label: __("Item"),
							in_list_view: 1,
							columns: 3,
							reqd: 1,
							get_query: () => ({ filters: { is_stock_item: 1, is_purchase_item: 1, disabled: 0, has_variants: 0 } }),
							// The unit the quantity is in, shown on the row (a Kg item is not typed in grams).
							onchange() {
								const row = this.doc;
								if (!row || !row.item_code) return;
								frappe.db.get_value("Item", row.item_code, "stock_uom").then((r) => {
									row.stock_uom = (r.message || {}).stock_uom || "";
									d.fields_dict.items.grid.refresh();
								});
							},
						},
						{ fieldname: "qty", fieldtype: "Float", label: __("Qty (stock unit)"), in_list_view: 1, columns: 2, reqd: 1 },
						{ fieldname: "stock_uom", fieldtype: "Data", label: __("Stock UOM"), in_list_view: 1, columns: 2, read_only: 1 },
						{ fieldname: "amount", fieldtype: "Currency", label: __("Amount Paid"), in_list_view: 1, columns: 3, reqd: 1 },
					],
				},
				{ fieldname: "mode_of_payment", fieldtype: "Select", label: __("Paid From"), options: data.modes.join("\n"), default: data.modes[0], reqd: 1 },
				{ fieldname: "supplier", fieldtype: "Select", label: __("Supplier"), options: suppliers, default: "" },
				{ fieldname: "remarks", fieldtype: "Data", label: __("Remarks") },
				{ fieldname: "receipt", fieldtype: "Attach", label: __("Receipt") },
			],
			primary_action_label: __("Save and Post"),
			primary_action: async (v) => {
				const items = (v.items || []).filter((r) => r.item_code).map((r) => ({ item_code: r.item_code, qty: r.qty, amount: r.amount }));
				if (!items.length) return frappe.msgprint(__("Add what was bought"));
				const sum = total(items);
				// What will be paid and received, said back before anything is posted.
				const info = await Promise.all(
					items.map((r) => frappe.db.get_value("Item", r.item_code, ["item_name", "stock_uom"]).then((x) => x.message || {}))
				);
				const supplier = v.supplier ? (suppliers.find((x) => x.value === v.supplier) || {}).label || v.supplier : "";
				const sure = await confirm_post([
					`<p>${__("{0} will be paid from the drawer «{1}» and received into {2}:", [`<b>${money(sum)}</b>`, esc(v.mode_of_payment), `<b>${esc(data.warehouse || "")}</b>`])}</p>`,
					`<ul>${items
						.map((r, i) => `<li>${esc(fmt_stock_qty(r.qty))} ${esc(__(info[i].stock_uom || ""))} ${esc(info[i].item_name || r.item_code)}: ${money(r.amount)}</li>`)
						.join("")}</ul>`,
					supplier ? `<p>${__("Supplier: {0}", [esc(supplier)])}</p>` : "",
					`<p><b>${__("Total: {0}", [money(sum)])}</b></p>`,
				]);
				if (!sure) return;
				// The manager's PIN is asked before the screen waits for the server.
				let ok = {};
				if (data.approval_above != null && sum > flt(data.approval_above) + 0.005) {
					ok = await ask_approval(this.floor, __("Cash purchase of {0} (above {1})", [money(sum), money(data.approval_above)]));
					if (!ok) return;
				}
				const send = () =>
					new Promise((resolve, reject) =>
						frappe.call({
							method: "resto.resto.shift.cash_purchase",
							args: { items: JSON.stringify(items), mode_of_payment: v.mode_of_payment, supplier: v.supplier || null, remarks: v.remarks || null, receipt: v.receipt || null, request_id, ...ok },
							callback: (r) => resolve(r.message),
							error: (r) => reject(r || {}),
						})
					);
				let pi = null;
				let approve = false;
				await this.shift_busy(d, async () => {
					try {
						pi = await send();
					} catch (r) {
						// A price far from the usual one (the server said why): a manager approves it.
						approve = r.exc_type === "ApprovalRequired" && !ok.approver && !this.floor.roles.manager;
					}
				});
				if (approve) {
					ok = await ask_approval(this.floor, __("Cash purchase of {0}", [money(sum)]));
					if (!ok) return;
					await this.shift_busy(d, async () => {
						pi = await send().catch(() => null);
					});
				}
				if (!pi) return;
				d.hide();
				frappe.show_alert({ message: __("Cash purchase recorded: {0}", [pi]), indicator: "green" });
			},
		});
		d.show();
	}

	// A salary advance paid to a worker out of the drawer (D-237): owed by them until it is taken
	// off their salary, so not an expense; the shift's expected cash goes down by it.
	async staff_advance() {
		const data = await frappe.xcall("resto.resto.staff_advance.advance_data");
		if (!data.modes.length) return frappe.msgprint(__("This shift has no cash payment method"));
		// One id per dialog: a retried click after a lost answer gives the same entry.
		const request_id = frappe.utils.get_random(20);
		if (!data.employees.length) return frappe.msgprint(__("No active worker is recorded as an Employee yet: a manager adds them first"));
		const name_of = (e) => (data.employees.find((x) => x.name === e) || {}).employee_name || e;
		const d = new frappe.ui.Dialog({
			title: __("Staff Advance from the Drawer"),
			fields: [
				{
					fieldname: "employee",
					fieldtype: "Select",
					label: __("Worker"),
					reqd: 1,
					options: [{ value: "", label: "" }, ...data.employees.map((e) => ({ value: e.name, label: e.employee_name || e.name }))],
				},
				{ fieldname: "mode_of_payment", fieldtype: "Select", label: __("Paid From"), options: data.modes.join("\n"), default: data.modes[0], reqd: 1 },
				{
					fieldname: "amount",
					fieldtype: "Currency",
					label: __("Amount"),
					reqd: 1,
					description: data.approval_above == null ? "" : __("Above {0} a manager approves", [money(data.approval_above)]),
				},
				{ fieldname: "remarks", fieldtype: "Data", label: __("Note") },
			],
			primary_action_label: __("Save and Post"),
			primary_action: async (v) => {
				const sure = await confirm_post([
					`<p>${__("{0} will be paid from the drawer «{1}» as an advance to {2}.", [`<b>${money(v.amount)}</b>`, esc(v.mode_of_payment), `<b>${esc(name_of(v.employee))}</b>`])}</p>`,
					`<p class="text-muted">${__("It is recorded as owed by the worker until it is taken off their salary or paid back; it is not an expense.")}</p>`,
					v.remarks ? `<p>${esc(v.remarks)}</p>` : "",
				]);
				if (!sure) return;
				// The manager's PIN is asked before the screen waits for the server.
				let ok = {};
				if (data.approval_above != null && flt(v.amount) > flt(data.approval_above) + 0.005) {
					ok = await ask_approval(this.floor, __("Staff advance of {0} to {1} (above {2})", [money(v.amount), name_of(v.employee), money(data.approval_above)]));
					if (!ok) return;
				} else if ((data.own || []).includes(v.employee)) {
					// The cashier's own advance: a manager's PIN whatever the amount (D-247).
					ok = await ask_approval(this.floor, __("Staff advance of {0} to {1}, the cashier's own", [money(v.amount), name_of(v.employee)]));
					if (!ok) return;
				}
				return this.shift_busy(d, async () => {
					const je = await frappe.xcall("resto.resto.staff_advance.add_advance", { ...v, request_id, ...ok });
					d.hide();
					frappe.show_alert({ message: __("Advance recorded: {0}", [je]), indicator: "green" });
				});
			},
		});
		d.show();
	}

	// Money put into the drawer (D-245): a staff advance paid back, change from the main safe, or
	// another reason from the accounts Resto Settings allows; never an account the cashier picks.
	async cash_in() {
		const data = await frappe.xcall("resto.resto.drawer_cash.cash_in_data");
		if (!data.modes.length) return frappe.msgprint(__("This shift has no cash payment method"));
		const reasons = [];
		if (data.workers.length) reasons.push({ value: "Staff Advance Repaid", label: __("Staff Advance Repaid") });
		if (data.safe) reasons.push({ value: "Change from the Main Safe", label: __("Change from the Main Safe") });
		if (data.accounts.length) reasons.push({ value: "Other", label: __("Other", null, "Resto") });
		if (!reasons.length) {
			return frappe.msgprint(__("Nothing can be put into the drawer yet: no worker owes an advance, and Resto Settings has no Main Safe Account or Drawer Cash In Accounts"));
		}
		// One id per dialog: a retried click after a lost answer gives the same entry.
		const request_id = frappe.utils.get_random(20);
		const worker = (e) => data.workers.find((w) => w.employee === e) || {};
		const d = new frappe.ui.Dialog({
			title: __("Cash In to the Drawer"),
			fields: [
				{ fieldname: "reason", fieldtype: "Select", label: __("Reason"), reqd: 1, options: [{ value: "", label: "" }, ...reasons] },
				{
					fieldname: "employee",
					fieldtype: "Select",
					label: __("Worker"),
					depends_on: "eval:doc.reason=='Staff Advance Repaid'",
					mandatory_depends_on: "eval:doc.reason=='Staff Advance Repaid'",
					options: [{ value: "", label: "" }, ...data.workers.map((w) => ({ value: w.employee, label: `${w.employee_name} · ${__("owes {0}", [money(w.owed)])}` }))],
				},
				{
					fieldname: "account",
					fieldtype: "Select",
					label: __("From"),
					depends_on: "eval:doc.reason=='Other'",
					mandatory_depends_on: "eval:doc.reason=='Other'",
					options: [{ value: "", label: "" }, ...data.accounts.map((a) => ({ value: a.name, label: a.account_name }))],
				},
				{ fieldname: "mode_of_payment", fieldtype: "Select", label: __("Into the Drawer"), options: data.modes.join("\n"), default: data.modes[0], reqd: 1 },
				{ fieldname: "amount", fieldtype: "Currency", label: __("Amount"), reqd: 1 },
				{ fieldname: "remarks", fieldtype: "Data", label: __("Note"), mandatory_depends_on: "eval:doc.reason=='Other'" },
			],
			primary_action_label: __("Save and Post"),
			primary_action: async (v) => {
				const w = worker(v.employee);
				if (v.reason === "Staff Advance Repaid" && flt(v.amount) > flt(w.owed) + 0.005) {
					return frappe.msgprint(__("{0} owes {1} only", [esc(w.employee_name), money(w.owed)]));
				}
				const from =
					v.reason === "Staff Advance Repaid"
						? __("paid back by {0} from their advance", [`<b>${esc(w.employee_name)}</b>`])
						: v.reason === "Change from the Main Safe"
						? __("from the main safe")
						: __("from {0}", [`<b>${esc((data.accounts.find((a) => a.name === v.account) || {}).account_name || v.account)}</b>`]);
				const sure = await confirm_post([
					`<p>${__("{0} will be put into the drawer «{1}», {2}.", [`<b>${money(v.amount)}</b>`, esc(v.mode_of_payment), from])}</p>`,
					v.remarks ? `<p>${esc(v.remarks)}</p>` : "",
				]);
				if (!sure) return;
				// Change and other reasons: the manager's PIN, asked before the screen waits.
				let ok = {};
				if (v.reason !== "Staff Advance Repaid" && data.needs_pin) {
					ok = await ask_approval(this.floor, __("Cash into the drawer: {0}", [money(v.amount)]));
					if (!ok) return;
				}
				return this.shift_busy(d, async () => {
					const je = await frappe.xcall("resto.resto.drawer_cash.add_cash_in", { ...v, request_id, ...ok });
					d.hide();
					frappe.show_alert({ message: __("Recorded: {0}", [je]), indicator: "green" });
				});
			},
		});
		d.show();
	}

	// Cash taken out of the drawer to the main safe (D-245), always with the manager's PIN: during the
	// shift (what should be in the drawer goes down) or right after the close (from what was counted).
	async cash_drop() {
		const data = await frappe.xcall("resto.resto.drawer_cash.drop_data");
		if (!data.modes.length) return frappe.msgprint(__("This shift has no cash payment method"));
		if (!data.safe) return frappe.msgprint(__("Set the Main Safe Account in Resto Settings first"));
		const request_id = frappe.utils.get_random(20);
		const left = (m) => (data.left || {})[m];
		const left_text = (m) => (data.after_close ? __("Left of the count: {0}", [money(left(m))]) : "");
		const d = new frappe.ui.Dialog({
			title: __("Cash Drop to the Main Safe"),
			fields: [
				{
					fieldname: "note_html",
					fieldtype: "HTML",
					options: data.after_close
						? `<p class="text-muted">${__("Shift {0} is closed: the drop is taken from what was counted, leaving the change for the next shift.", [esc(data.shift)])}</p>`
						: "",
				},
				{
					fieldname: "mode_of_payment",
					fieldtype: "Select",
					label: __("From the Drawer"),
					options: data.modes.join("\n"),
					default: data.modes[0],
					reqd: 1,
					description: left_text(data.modes[0]),
					onchange() {
						if (data.after_close) d.set_df_property("mode_of_payment", "description", left_text(d.get_value("mode_of_payment")));
					},
				},
				{ fieldname: "amount", fieldtype: "Currency", label: __("Amount"), reqd: 1 },
				{ fieldname: "remarks", fieldtype: "Data", label: __("Note") },
			],
			primary_action_label: __("Save and Post"),
			primary_action: async (v) => {
				if (data.after_close && flt(v.amount) > flt(left(v.mode_of_payment)) + 0.005) {
					return frappe.msgprint(__("Only {0} is left of what was counted for {1}", [money(left(v.mode_of_payment)), esc(v.mode_of_payment)]));
				}
				const sure = await confirm_post([
					`<p>${__("{0} will be taken from the drawer «{1}» to the main safe.", [`<b>${money(v.amount)}</b>`, esc(v.mode_of_payment)])}</p>`,
					v.remarks ? `<p>${esc(v.remarks)}</p>` : "",
				]);
				if (!sure) return;
				let ok = {};
				if (data.needs_pin) {
					ok = await ask_approval(this.floor, __("Cash drop of {0} to the main safe", [money(v.amount)]));
					if (!ok) return;
				}
				return this.shift_busy(d, async () => {
					const je = await frappe.xcall("resto.resto.drawer_cash.cash_drop", { ...v, request_id, ...ok });
					d.hide();
					frappe.show_alert({ message: __("Recorded: {0}", [je]), indicator: "green" });
				});
			},
		});
		d.show();
	}

	// A customer pays what they owe (a bill left on account): a Payment Entry against their unpaid
	// bills, oldest first, counted in this shift's drawer.
	collect_debt() {
		const request_id = frappe.utils.get_random(20);
		let debt = null;
		const d = new frappe.ui.Dialog({
			title: __("Collect Debt"),
			fields: [
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer"), reqd: 1, onchange: () => load() },
				{ fieldname: "owed", fieldtype: "HTML" },
				{ fieldname: "amount", fieldtype: "Currency", label: __("Amount Paid"), reqd: 1 },
				{ fieldname: "mode_of_payment", fieldtype: "Select", label: __("Mode of Payment"), reqd: 1 },
			],
			primary_action_label: __("Collect"),
			primary_action: async (v) => {
				const who = (debt && debt.customer_name) || v.customer;
				const sure = await confirm_post([
					`<p>${__("{0} will be received from {1} into «{2}», against their oldest unpaid bills first.", [`<b>${money(v.amount)}</b>`, `<b>${esc(who)}</b>`, esc(v.mode_of_payment)])}</p>`,
				]);
				if (!sure) return;
				return this.shift_busy(d, async () => {
					const pe = await frappe.xcall("resto.resto.shift.collect_debt", { ...v, request_id });
					d.hide();
					frappe.show_alert({ message: __("Payment recorded: {0}", [pe]), indicator: "green" });
				});
			},
		});
		const load = async () => {
			const customer = d.get_value("customer");
			if (!customer) return d.fields_dict.owed.$wrapper.html("");
			debt = await frappe.xcall("resto.resto.shift.customer_debt", { customer });
			if (customer !== d.get_value("customer")) return; // changed meanwhile
			d.set_df_property("mode_of_payment", "options", debt.modes.join("\n"));
			if (!d.get_value("mode_of_payment")) d.set_value("mode_of_payment", debt.modes[0]);
			d.fields_dict.owed.$wrapper.html(
				debt.total > 0.005
					? `<p><b>${__("Owes {0}", [money(debt.total)])}</b></p>
						<table class="table table-condensed">${debt.invoices
							.map((i) => `<tr><td>${esc(frappe.datetime.str_to_user(i.posting_date))}</td><td>${esc(i.name)}</td><td class="rf-amt">${money(i.outstanding_amount)}</td></tr>`)
							.join("")}</table>`
					: `<p class="text-muted">${__("{0} owes nothing", [esc(debt.customer_name || customer)])}</p>`
			);
			d.set_value("amount", debt.total > 0.005 ? debt.total : 0);
		};
		d.show();
	}

	// The open shift so far, read only (D-262): nothing is counted or written. Printed on the
	// browser's printer with a banner that it is not a close.
	async shift_summary() {
		const data = await frappe.xcall("resto.resto.shift.shift_summary");
		const d = new frappe.ui.Dialog({ title: __("Shift Summary Now"), size: "large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		const html = this.shift_report(data, false);
		d.fields_dict.html.$wrapper.html(html);
		if (!data.blind) {
			d.set_primary_action(__("Print"), () => {
				const w = window.open("", "_blank");
				if (!w) return frappe.msgprint(__("Allow pop-ups for this site to print"));
				const rtl = frappe.utils.is_rtl ? frappe.utils.is_rtl() : document.documentElement.dir === "rtl";
				w.document.write(`<!doctype html><html dir="${rtl ? "rtl" : "ltr"}"><head><meta charset="utf-8"><title>${esc(data.shift)}</title>
					<style>body{font-family:Almarai,Tahoma,sans-serif;direction:${rtl ? "rtl" : "ltr"};margin:12mm;font-size:12px}
					.banner{border:2px solid #000;padding:6px;text-align:center;font-weight:800;font-size:15px;margin-bottom:8px}
					table{width:100%;border-collapse:collapse;margin:6px 0}td,th{border-bottom:1px solid #ccc;padding:3px 4px;text-align:start}
					.rf-amt{text-align:end}.rf-metrics{display:flex;flex-wrap:wrap;gap:6px}.rf-metric{border:1px solid #ccc;padding:4px 6px;min-width:30%}
					details>summary{list-style:none;font-weight:700}.text-muted,.small{color:#555}</style></head>
					<body><div class="banner">${__("Interim summary: the shift is not closed")}</div><h3>${esc(data.shift)}</h3>${html}</body></html>`);
				w.document.close();
				w.document.querySelectorAll("details").forEach((x) => (x.open = true));
				w.focus();
				w.print();
			});
		}
		d.show();
	}

	async close_shift() {
		const data = await frappe.xcall("resto.resto.shift.closing_data");
		const d = new frappe.ui.Dialog({ title: __("Close Shift"), size: "large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		const $w = d.fields_dict.html.$wrapper;
		$w.html(this.shift_report(data, true));
		const diff_html = (n) =>
			Math.abs(n) < 0.005
				? `<span class="text-success">✓ ${__("Balanced")}</span>`
				: `<span class="${n < 0 ? "text-danger" : "text-warning"}">${n < 0 ? __("Short {0}", [money(-n)]) : __("Over {0}", [money(n)])}</span>`;
		const counted = () => data.rows.map((r, i) => $w.find(`[data-m="${i}"]`).val());
		// The difference as the cashier types, unless the count is blind (Restaurant Settings).
		$w.find("[data-m]").on("input", () => {
			if (data.blind) return;
			const vals = counted();
			data.rows.forEach((row, i) => {
				$w.find(`[data-d="${i}"]`).html(vals[i] === "" ? "" : diff_html(flt(vals[i]) - flt(row.expected_amount)));
			});
			const all = vals.every((v) => v !== "");
			const tc = vals.reduce((a, v) => a + flt(v), 0);
			const te = data.rows.reduce((a, r) => a + flt(r.expected_amount), 0);
			$w.find("[data-tc]").html(all ? money(tc) : "");
			$w.find("[data-td]").html(all ? diff_html(tc - te) : "");
		});
		d.set_primary_action(__("Close Shift"), () => {
			const vals = counted();
			if (vals.some((v) => v === "")) return frappe.msgprint(__("Please enter all closing amounts"));
			const amounts = {};
			data.rows.forEach((r, i) => (amounts[r.mode_of_payment] = flt(vals[i])));
			// Asked once more: a closed shift cannot be opened again.
			const notes = [];
			if (!data.blind) {
				const short = data.rows.reduce((a, r, i) => a + Math.min(0, flt(vals[i]) - flt(r.expected_amount)), 0);
				if (short < -0.005) notes.push(`<p class="text-danger">${__("The drawer is short by {0}.", [money(-short)])}</p>`);
			}
			if (data.open_orders) notes.push(`<p class="text-warning">${__("{0} orders are still open: their bills are not in this shift.", [data.open_orders])}</p>`);
			frappe.confirm(`${notes.join("")}<p>${__("Close the shift? A closed shift cannot be opened again.")}</p>`, () =>
				this.shift_busy(d, async () => {
					const res = await frappe.xcall("resto.resto.shift.close_shift", { amounts: JSON.stringify(amounts) });
					d.hide();
					const done = new frappe.ui.Dialog({ title: __("Shift closed: {0}", [res.closing]), size: "large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
					done.fields_dict.html.$wrapper.html(this.shift_report(res, false));
					// The cash counted taken to the safe now, leaving the change for tomorrow (D-245).
					done.set_secondary_action_label(__("Cash Drop to the Main Safe"));
					done.set_secondary_action(() => this.cash_drop());
					// The closing report on paper, for the manager to sign (Resto Shift Close).
					done.set_primary_action(__("Print"), () =>
						window.open(
							`/printview?doctype=${encodeURIComponent("Resto Shift")}&name=${encodeURIComponent(res.closing)}&format=${encodeURIComponent("Resto Shift Close")}&trigger_print=1`
						)
					);
					done.show();
					this.refresh();
				})
			);
		});
		d.show();
	}

	// ------------------------------------------------------------------ order panel

	async load_order(name) {
		if (this.order && this.order.name !== name) this.flush_taps();
		this._load_seq = (this._load_seq || 0) + 1;
		const seq = this._load_seq;
		const gen = this._gen;
		const order = await call("get_order", { order: name }, false, 20000);
		// Another order was opened, or this one left, while this one was on its way.
		if (seq !== this._load_seq) return;
		if (!["Open", "Bill Requested"].includes(order.status)) {
			this.close_order();
			frappe.show_alert({ message: __("Order {0} is {1}", [name, __(order.status, null, "Resto")]), indicator: "blue" });
			return;
		}
		// The same order already on screen: its title and lines only; the menu (images, search,
		// chosen group) stays as it is (D-210).
		const shown = this.order && this.order.name === name && this.$order.is(":visible") && this.$order.find(".rf-menu").length;
		// Changed on the screen while this was on its way: the answer to that change draws it.
		if (shown && (gen !== this._gen || this.syncing())) return;
		this.order = order;
		if (!this.menu) this.menu = await call("get_menu");
		if (shown) {
			this.$order.find(".rf-otitle").html(this.order_title(order));
			return this.render_lines();
		}
		this.render_order();
	}

	order_title(o) {
		const where = o.staff_meal
			? `${__("Staff Meal", null, "Resto")}${o.staff_for ? ` · ${esc(o.staff_for)}` : ""}`
			: o.order_type === "Dine In"
			? __("Table {0}", [esc(o.resto_table)], "Resto")
			: __(o.order_type, null, "Resto");
		// The order's number of the day first, as on its kitchen tickets and bill (D-221).
		const head = o.order_no ? `<span class="rf-ono">${__("Order {0}", [o.order_no], "Resto")}</span> · ${where}` : where;
		const sub = [esc(o.name)];
		if (o.order_type === "Dine In") sub.push(__("{0} guests", [o.guests]));
		if (o.opened_at) sub.push(__("for {0}", [dur(minutes_since(o.opened_at))]));
		return `<strong>${head}${o.status === "Bill Requested" ? ` <span class="rf-tag t-bill">${__("Bill Requested")}</span>` : ""}</strong>
			<small>${sub.join(" · ")}</small>`;
	}

	async reload_lines(name) {
		// Periodic refresh: update the order's lines and totals but keep the menu, its search and
		// any open dialog as they are. Not while changes made on the screen are still on their way
		// or were made while this was: the screen shows them, and the server's answer replaces it.
		if (this.syncing()) return;
		const gen = this._gen;
		const order = await call("get_order", { order: name }, false, 20000);
		if (!this.order || this.order.name !== name) return;
		if (!["Open", "Bill Requested"].includes(order.status)) return this.load_order(name);
		if (gen !== this._gen || this.syncing()) return;
		const status_changed = order.status !== this.order.status;
		this.order = order;
		if (status_changed) this.$order.find(".rf-otitle").html(this.order_title(order));
		this.render_lines();
	}

	syncing() {
		return !!((this._taps || []).length || outbox().length);
	}

	// The order as the server returned it after a change (D-210): only the lines are drawn again.
	show_view(view) {
		if (!this.order || this.order.name !== view.name || this.syncing()) return;
		if (!["Open", "Bill Requested"].includes(view.status)) return this.load_order(view.name);
		const status_changed = view.status !== this.order.status;
		this.order = view;
		if (status_changed) this.$order.find(".rf-otitle").html(this.order_title(view));
		this.render_lines();
	}

	// A dish tapped (D-210): on the screen at once, then sent with the other taps of the same moment
	// in one call. The server stays the record (kitchen, other screens, a closed browser): its answer
	// replaces what the screen guessed, and a refusal puts the server's order back.
	tap_item(item) {
		if (this._printing) return this.printing_wait();
		if (!this.floor.can_order) return frappe.msgprint(esc(this.floor.no_order_reason));
		const o = this.order;
		const same = o.items.find((d) => d.item_code === item.item_code && plain_line(d));
		if (same) {
			same.qty = flt(same.qty) + 1;
			same.amount = flt(same.amount) + flt(item.rate);
		} else {
			o.items.push({
				name: `__new_${frappe.utils.get_random(8)}`,
				item_code: item.item_code,
				item_name: item.item_name,
				qty: 1,
				rate: item.rate,
				amount: flt(item.rate),
				kitchen_status: "Not Sent",
				modifiers: "[]",
				_pending: 1,
			});
		}
		o.net_total = flt(o.net_total) + flt(item.rate);
		this._gen++;
		this._show_code = item.item_code;
		if (this._tap_order && this._tap_order !== o.name) this.flush_taps();
		this._tap_order = o.name;
		this._taps = this._taps || [];
		this._taps.push(item.item_code);
		frappe.utils.play_sound("click");
		this.render_lines();
		// Sent 300 ms after the first tap of a run, not the last: the line is saved sooner.
		if (!this._tap_timer) this._tap_timer = setTimeout(() => this.flush_taps(), 300);
	}

	// Taps not sent yet go now, in one call, before anything else is done with the order. Resolves
	// once the queue is sent, or left for later when there is no connection.
	flush_taps(send = true) {
		clearTimeout(this._tap_timer);
		this._tap_timer = null;
		const taps = this._taps || [];
		this._taps = [];
		if (!taps.length) return send ? this.drain() : Promise.resolve();
		const counts = {};
		taps.forEach((c) => (counts[c] = (counts[c] || 0) + 1));
		const items = Object.entries(counts).map(([item_code, qty]) => ({ item_code, qty }));
		const name_of = (code) => ((this.order && this.order.items.find((d) => d.item_code === code)) || {}).item_name || code;
		const label = items.map((i) => `${i.qty} × ${name_of(i.item_code)}`).join("، ");
		const order = this._tap_order || this.order.name;
		this._tap_order = null;
		return this.sync_call("add_items", { order, items }, label, null, !send);
	}

	// A change is put in the outbox and the outbox is sent, in order.
	sync_call(method, args, label, extra, later) {
		set_outbox([...outbox(), { op: frappe.utils.get_random(20), method, args, label: label || "", at: Date.now(), ...(extra || {}) }]);
		if (this.offline) this.set_offline(true); // the count on the bar
		return later ? Promise.resolve() : this.drain();
	}

	// Sends the outbox one change after the other. Never rejects. No connection: the changes stay
	// (the screen keeps showing them) and are sent again in a few seconds or when it is back. A
	// refusal: dropped, with a message, and the order is drawn again as the server has it.
	drain() {
		if (this._draining) return this._draining;
		const run = (async () => {
			// Never done before this._draining holds it: an empty outbox (or a change waiting for the
			// waiter's choice) used to end the body at once, its finally ran first, and the finished
			// promise stayed in this._draining, so nothing was ever sent again from this page.
			await null;
			try {
				for (;;) {
					const list = outbox();
					if (!list.length) break;
					const job = list[0];
					if (!job || typeof job !== "object" || !job.op || !job.method || !job.args || !job.args.order) {
						set_outbox(list.slice(1));
						continue; // not a change this screen made: dropped
					}
					// Kept more than two minutes (offline, or left by a closed page): the dishes may
					// have been taken again on another screen meanwhile, so the waiter decides.
					if (!job.ok && Date.now() - cint(job.at) > 120000) {
						this.ask_stale();
						break;
					}
					let view = null;
					this._sending_op = job.op;
					try {
						view = await call(job.method, { ...job.args, view: 1, op: job.op }, false, 20000);
					} catch (e) {
						if (e && e.network) {
							// The server answered without refusing (busy or failing: 5xx, 429, 408):
							// connected, the change waits and is sent again, and an action on the order
							// says so. No answer, or not the server's (a portal page, a login): offline.
							this._fail = { op: job.op, status: cint(e.status), at: Date.now() };
							this.set_offline(!server_busy(e));
							clearTimeout(this._retry);
							this._retry = setTimeout(() => this.drain(), 5000);
							break;
						}
						this._refusals = (this._refusals || 0) + 1;
						this.refused(job);
					} finally {
						this._sending_op = null;
					}
					this._fail = null;
					set_outbox(outbox().filter((j) => j.op !== job.op));
					this.set_offline(false);
					this.pending_gone();
					try {
						if (this.order && this.order.name === job.args.order) {
							if (view) this.show_view(view);
							else await this.reload_lines(job.args.order);
						}
					} catch (e) {
						// the next refresh draws it
					}
				}
			} catch (e) {
				console.error(e);
			} finally {
				if (this._draining === run) this._draining = null;
			}
		})();
		this._draining = run;
		return run;
	}

	// A change kept more than two minutes waits for the waiter's choice (asked again a minute after
	// the window was closed without one; an action on the order asks at once).
	ask_stale() {
		if (Date.now() < (this._snooze || 0)) return;
		this.show_pending();
	}

	// The window of changes not sent closes once none of its changes waits any more (sent by a
	// retry, or by another tab of this browser).
	pending_gone() {
		const d = this._pending_dialog;
		if (!d) return;
		const left = new Set(outbox().map((j) => j && j.op));
		if (!d.resto_ops.some((op) => left.has(op))) d.hide();
	}

	// The changes kept on this device, what holds them, and the choice: send them now, or discard
	// them (taken again on another screen meanwhile). One window at a time; closed without a choice,
	// the next action on the order or the next refresh asks again.
	show_pending() {
		const jobs = outbox().filter((j) => j && typeof j === "object" && j.op);
		if (!jobs.length) return;
		if (this._pending_dialog) return; // one window (display is set only once it has opened)
		const old = jobs.some((j) => !j.ok && Date.now() - cint(j.at) > 120000);
		const fail = this._fail && jobs.some((j) => j.op === this._fail.op) ? this._fail : null;
		const why = old
			? __("If they were taken again on another screen, discard them.")
			: fail
			? __("The server did not take them yet (error {0}): they are sent again by themselves, or now.", [fail.status])
			: __("They are being sent: try again in a moment, or send them now.");
		const rows = jobs
			.map((j) => {
				const mins = Math.max(0, Math.round((Date.now() - cint(j.at)) / 60000));
				const what = (j.label || (j.method === "remove_line" ? __("a line removed") : __("a quantity changed"))) + (j.kept ? ` (${__("printed for the kitchen, kept")})` : "");
				return `<li>${__("Made {0} min ago on order {1} and not sent yet: {2}", [mins, esc((j.args || {}).order), esc(what)])}</li>`;
			})
			.join("");
		const orders = [...new Set(jobs.map((j) => (j.args || {}).order))];
		let chose = false;
		const d = (this._pending_dialog = new frappe.ui.Dialog({
			title: __("Changes Not Sent Yet"),
			fields: [{ fieldname: "html", fieldtype: "HTML", options: `<ul>${rows}</ul><p class="text-muted">${why}</p>` }],
			primary_action_label: __("Send Now"),
			primary_action: async () => {
				chose = true;
				const ops = new Set(jobs.map((j) => j.op));
				set_outbox(outbox().map((j) => (j && ops.has(j.op) ? { ...j, ok: 1 } : j)));
				d.hide();
				clearTimeout(this._retry);
				const refusals = this._refusals || 0;
				await this.drain();
				// "Sent" only when none was refused (a refusal says so itself).
				if (!outbox().length) (this._refusals || 0) === refusals && frappe.show_alert({ message: __("Changes sent"), indicator: "green" });
				else if (this.offline) frappe.msgprint(__("Not connected: the last changes are kept on this device and sent when the connection is back. Try again then."));
				else this.show_pending();
			},
			secondary_action_label: __("Discard"),
			secondary_action: () => {
				chose = true;
				// The change on its way to the server now cannot be called back: it is not discarded.
				const busy = this._sending_op;
				// What was printed for the kitchen was cooked: it stays, to be billed.
				const kept = new Set(outbox().filter((j) => j && j.kept).map((j) => j.op));
				const ops = new Set(jobs.map((j) => j.op).filter((op) => op !== busy && !kept.has(op)));
				set_outbox(outbox().filter((j) => !(j && ops.has(j.op))));
				d.hide();
				if (jobs.some((j) => kept.has(j.op))) {
					frappe.show_alert({ message: __("The dishes printed for the kitchen are kept: they are sent when the connection is back."), indicator: "orange" }, 8);
				}
				if (busy && jobs.some((j) => j.op === busy)) {
					frappe.show_alert({ message: __("A change already on its way to the server was not discarded"), indicator: "orange" });
				}
				if (this.order && orders.includes(this.order.name)) this.reload_lines(this.order.name).catch(() => {});
				if (this.offline) this.set_offline(true); // the count on the bar
			},
			on_hide: () => {
				if (this._pending_dialog === d) this._pending_dialog = null;
				if (!chose) this._snooze = Date.now() + 60000;
			},
		}));
		d.resto_ops = jobs.map((j) => j.op);
		d.show();
	}

	refused(job) {
		if (job.method !== "add_items") return; // the server's message says it; the order is drawn again
		frappe.msgprint({
			title: __("Not Saved"),
			indicator: "red",
			message: __("These dishes were not added to order {0}: {1}", [esc(job.args.order), esc(job.label)]),
		});
		// A dish taken off the menu since: the menu is read again.
		call("get_menu")
			.then((menu) => (this.menu = menu))
			.catch(() => {});
	}

	// No connection (D-213): a bar on the screen says so and how many changes wait on this device.
	set_offline(flag) {
		const was = this.offline;
		this.offline = flag;
		if (this.floor && was !== flag) $(this.page.body).find(".rf-net").replaceWith(this.net_pill());
		let $bar = $(this.page.body).find(".rf-offline");
		$(this.page.body).find(".rf-root").toggleClass("rf-off", !!flag);
		if (!flag) {
			$bar.remove();
			if (was) frappe.show_alert({ message: __("Connected again"), indicator: "green" });
			return;
		}
		if (!$bar.length) $bar = $(`<div class="rf-offline"></div>`).prependTo($(this.page.body).find(".rf-root"));
		const n = outbox().length;
		$bar.text(
			n
				? __("Not connected: {0} change(s) are kept on this device and sent when the connection is back. Sending to the kitchen, bills and payments wait for it.", [n])
				: __("Not connected: sending to the kitchen, bills and payments wait for the connection.")
		);
	}

	// Before an action on the order: the taps and changes waiting are sent first. false: they could
	// not be (no connection), and the action is not done, so the kitchen or the bill never misses them.
	// "Not connected" only when the server gave no answer; otherwise what waits is shown, with the
	// choice to send it now or discard it.
	async settled() {
		await this.flush_taps();
		if (!outbox().length) return true;
		if (this.offline) {
			frappe.msgprint(__("Not connected: the last changes are kept on this device and sent when the connection is back. Try again then."));
		} else {
			this.show_pending();
		}
		return false;
	}

	close_order() {
		this._inv = null;
		this.$order.off(".rfinv");
		this.flush_taps();
		this._load_seq = (this._load_seq || 0) + 1; // an order still loading is not shown
		this.order = null;
		this.$order.hide().empty();
		$(this.page.body).find(".resto-floor").show();
		this.refresh();
	}

	// Back to the floor: dishes not sent to the kitchen yet are named first.
	leave_order() {
		const o = this.order;
		const unsent = o ? o.items.filter((d) => d.kitchen_status === "Not Sent" && !d.hold).reduce((n, d) => n + flt(d.qty), 0) : 0;
		if (!unsent) return this.close_order();
		frappe.confirm(__("{0} dish(es) on this order are not sent to the kitchen yet. Go back to the floor anyway?", [fmt_qty(unsent)]), () =>
			this.close_order()
		);
	}

	render_order() {
		const o = this.order;
		$(this.page.body).find(".resto-floor").hide();
		this.$order.css("display", "flex").html(`
			<header class="rf-head rf-ohead">
				<button class="rf-btn rf-back">${ICON.back}<span>${__("Floor", null, "Resto")}</span></button>
				<div class="rf-otitle">${this.order_title(o)}</div>
				<span class="rf-spacer"></span>
				${o.staff_meal ? "" : `<div class="rf-custchip"></div>`}
				<span class="rf-hide-sm">${this.shift_pill()}</span>
				${this.net_pill()}
			</header>
			<div class="rf-ocols">
				<section class="rf-menu">
					<div class="rf-srow">
						<label class="rf-sbox">${ICON.search}<input type="search" class="rf-search" autocomplete="off"
							placeholder="${__("Search by name or code, or scan a barcode")}"><kbd dir="ltr">F4</kbd></label>
						<button class="rf-ib rf-scan" title="${__("Scan Barcode")}" aria-label="${__("Scan Barcode")}">${ICON.barcode}</button>
					</div>
					<div class="rf-groups"></div>
					<div class="rf-items"></div>
				</section>
				<aside class="rf-cart">
					<div class="rf-chead"><b>${__("Order", null, "Resto")}</b><span class="rf-ccount"></span><span class="rf-spacer"></span>
						<button class="rf-btn rf-sm" data-act="note">${__("Note")}</button>
						${o.staff_meal ? `<button class="rf-btn rf-sm" data-act="staff_item">${__("Free Ingredient")}</button>` : `<button class="rf-btn rf-sm" data-act="discount">${__("Discount")}</button>`}
						<button class="rf-ib rf-sm" data-act="more" title="${__("More")}" aria-label="${__("More")}">${ICON.more}</button></div>
					<div class="rf-lines"></div>
					<div class="rf-cfoot"><div class="rf-totals"></div><div class="rf-cbtns">
						${o.staff_meal ? (this.floor.roles.cashier || this.floor.roles.manager ? `<button class="rf-btn rf-btn-p rf-xl" data-act="staff_post">${ICON.send}<span>${__("Send and Post")}</span></button>` : `<small class="rf-muted">${__("Only a cashier or a manager makes a staff meal")}</small>`) : `
						<button class="rf-btn rf-lg rf-fire" data-act="fire" style="display:none"></button>
						<div class="rf-c2">
							<button class="rf-btn rf-btn-d rf-lg" data-act="send">${ICON.send}<span class="rf-send-t"></span></button>
							<button class="rf-btn rf-lg" data-act="bill">${ICON.print}<span>${__("Print Bill")}</span></button>
						</div>
						${this.floor.roles.cashier || this.floor.roles.manager ? `<button class="rf-btn rf-btn-p rf-xl rf-pay-b" data-act="pay"></button>` : ""}`}
					</div></div>
				</aside>
			</div>`);
		this.$order.find(".rf-back").on("click", () => this.leave_order());
		this.$order.find(".rf-ohead [data-shift]").on("click", (e) => this.shift_click($(e.currentTarget).attr("data-shift")));
		this.$order.find(".rf-scan").on("click", () => this.$order.find(".rf-search").trigger("focus").select());
		// One handler for the order's buttons; each goes through action() (changes sent first, one at a time).
		this.$order.off("click.rfact").on("click.rfact", "[data-act]", (e) => this.action($(e.currentTarget).attr("data-act"), e));
		this.$order.off("click.rfpack").on("click.rfpack", "[data-pack]", (e) =>
			this.set_pack($(e.currentTarget).attr("data-pack"), cint($(e.currentTarget).attr("data-n")))
		);
		this.$order.off("click.rfcust").on("click.rfcust", "[data-cust]", (e) =>
			this.action($(e.currentTarget).attr("data-cust") === "new" ? "new_customer" : "customer")
		);
		this.render_menu();
		this.render_lines();
	}

	// The customer on the order, in the order's header.
	render_customer() {
		const o = this.order;
		const staff = this.floor.roles.cashier || this.floor.roles.manager;
		this.$order.find(".rf-custchip").html(`
			<span class="rf-av b">${ICON.user}</span>
			<div class="rf-custtxt"><b>${esc(o.customer_name || __("Walk-in", null, "Resto"))}</b><small>${o.phone ? `<bdi dir="ltr">${esc(o.phone)}</bdi>` : __("Customer")}</small></div>
			<button class="rf-ib rf-flat" data-cust="find" title="${__("Change Customer")}" aria-label="${__("Change Customer")}">${ICON.search}</button>
			${staff ? `<button class="rf-ib rf-flat g" data-cust="new" title="${__("New Customer")}" aria-label="${__("New Customer")}">${ICON.plus}</button>` : ""}`);
	}

	render_menu(group, search) {
		const $groups = this.$order.find(".rf-groups");
		this._group = group === undefined ? this._group || null : group;
		const groups = this.menu.groups;
		$groups.html(
			[`<button class="rf-chip ${!this._group ? "on" : ""}" data-g="">${__("All")}</button>`]
				.concat(groups.map((g) => `<button class="rf-chip ${this._group === g ? "on" : ""}" data-g="${esc(g)}">${esc(__(g))}</button>`))
				.join("")
		);
		$groups.find("button").on("click", (e) => this.render_menu($(e.currentTarget).attr("data-g") || null));
		const $search = this.$order.find(".rf-search");
		$search.off("input").on("input", frappe.utils.debounce(() => this.render_menu(undefined), 200));
		// Enter (a barcode scanner ends with it): the dish whose code is typed, or the only one found.
		$search.off("keydown").on("keydown", (e) => {
			if (e.key !== "Enter") return;
			e.preventDefault();
			const typed = ($search.val() || "").trim();
			if (!typed) return;
			const txt = typed.toLowerCase();
			// A scanned barcode or a typed code first, then the only dish found.
			const item =
				this.menu.items.find((i) => (i.barcodes || []).includes(typed)) ||
				this.menu.items.find((i) => i.item_code.toLowerCase() === txt) ||
				(() => {
					const found = this.menu_matches(txt);
					return found.length === 1 ? found[0] : null;
				})();
			if (!item) return frappe.show_alert({ message: __("No single dish matches {0}", [esc(txt)]), indicator: "orange" });
			$search.val("");
			this.render_menu(undefined);
			this.add_item(item);
		});
		const txt = ($search.val() || "").trim().toLowerCase();
		const items = this.menu_matches(txt);
		const shade = (g) => `c${Math.max(groups.indexOf(g), 0) % 6}`;
		this.$order.find(".rf-items").html(
			items
				.map(
					(i) => `<button class="rf-item" data-item="${esc(i.item_code)}">
					<span class="rf-bdg" style="display:none"></span>
					${i.image ? `<img src="${esc(i.image)}" loading="lazy" alt="">` : `<span class="rf-ph ${shade(i.item_group)}">${esc((i.item_name || "?").trim().charAt(0))}</span>`}
					<span class="rf-iname">${esc(i.item_name)}</span>
					<span class="rf-irate">${money(i.rate)}${i.has_modifiers ? " ✱" : ""}</span></button>`
				)
				.join("") || `<p class="rf-empty">${__("No items. Check prices in the price list {0}.", [esc(this.menu.price_list)])}</p>`
		);
		this.$order.find(".rf-item").on("click", (e) => {
			const item = this.menu.items.find((i) => i.item_code === $(e.currentTarget).attr("data-item"));
			this.add_item(item);
		});
		this.update_badges();
	}

	menu_matches(txt) {
		// أ إ آ ا, ة ه, ى ي and the short vowels match each other: a cashier types them either way.
		const norm = (v) =>
			String(v || "")
				.toLowerCase()
				.replace(/[\u064B-\u0652\u0640]/g, "")
				.replace(/[أإآ]/g, "ا")
				.replace(/ة/g, "ه")
				.replace(/ى/g, "ي");
		const t = norm(txt);
		return this.menu.items.filter(
			(i) =>
				(!this._group || i.item_group === this._group || txt) &&
				(!t || norm(i.item_name).includes(t) || norm(i.item_code).includes(t) || (i.barcodes || []).includes(txt))
		);
	}

	// How many of each dish the order has, on the menu's cards.
	update_badges() {
		const counts = {};
		(this.order.items || []).forEach((d) => {
			if (d.kitchen_status !== "Void") counts[d.item_code] = (counts[d.item_code] || 0) + flt(d.qty);
		});
		this.$order.find(".rf-item").each((_, el) => {
			const n = counts[$(el).attr("data-item")];
			$(el).find(".rf-bdg").text(n ? fmt_qty(n) : "").toggle(!!n);
		});
	}

	async add_item(item) {
		if (!item.has_modifiers) return this.tap_item(item);
		if (!this.floor.can_order) return frappe.msgprint(esc(this.floor.no_order_reason));
		const groups = await call("get_modifiers", { item_code: item.item_code });
		const fields = [{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), default: 1, reqd: 1 }];
		groups.forEach((g, gi) => {
			fields.push({
				fieldtype: "Section Break",
				label:
					esc(g.name) +
					(g.min_select ? ` (${__("required")})` : "") +
					(g.max_select ? ` · ${__("max {0}", [g.max_select])}` : ""),
			});
			g.options.forEach((opt, oi) => {
				fields.push({
					fieldname: `m_${gi}_${oi}`,
					fieldtype: "Check",
					label: esc(opt.option_name) + (opt.price ? ` (+${money(opt.price)})` : ""),
					default: opt.is_default,
				});
			});
		});
		fields.push(
			{ fieldtype: "Section Break" },
			{ fieldname: "notes", fieldtype: "Small Text", label: __("Kitchen Note") },
			{ fieldname: "course", fieldtype: "Select", options: COURSES.map((c) => ({ value: c, label: __(c) })), label: __("Course", null, "Resto") },
			{ fieldname: "hold", fieldtype: "Check", label: __("Hold Until Fired") }
		);
		const d = new frappe.ui.Dialog({
			title: esc(item.item_name),
			fields,
			primary_action_label: __("Add", null, "Resto"),
			primary_action: async (v) => {
				const modifiers = [];
				groups.forEach((g, gi) =>
					g.options.forEach((opt, oi) => {
						if (v[`m_${gi}_${oi}`]) modifiers.push({ group: g.name, option: opt.option_name });
					})
				);
				if (d._busy) return;
				d._busy = true;
				try {
					if (!(await this.settled())) return;
					const name = this.order.name;
					const items = [{ item_code: item.item_code, qty: v.qty, notes: v.notes, course: v.course, hold: v.hold, modifiers }];
					// Pressed again after a dropped answer: added once; changed choices are a new dish.
					const key = JSON.stringify(items);
					if (d._op_key !== key) {
						d._op = frappe.utils.get_random(20);
						d._op_key = key;
					}
					const view = await call("add_items", { order: name, items, view: 1, op: d._op }, false, 20000);
					d.hide();
					frappe.utils.play_sound("click");
					if (this.order && this.order.name === name) this.show_view(view);
				} catch (e) {
					// refused or not connected: the message is shown and the dialog keeps the choices
				} finally {
					d._busy = false;
				}
			},
		});
		d.show();
	}

	render_lines() {
		const o = this.order;
		const manager = this.floor.roles.manager;
		// Where each sent dish is (D-220): at the kitchen, its ticket printed, being made, ready.
		const in_kitchen = (d) =>
			d.kitchen_status === "Sent"
				? d.printed
					? `${__("Printed")} ✓`
					: `${__("At the Kitchen")} ✓`
				: d.kitchen_status === "In Progress"
				? __("In Progress", null, "Resto")
				: null;
		const lines = o.items.map((d) => {
			const sent = d.kitchen_status !== "Not Sent";
			const voided = d.kitchen_status === "Void";
			const each = flt(d.qty) ? flt(d.amount) / flt(d.qty) : flt(d.rate);
			const extra = [d.modifiers_text, d.sides_text, d.extras_text].filter(Boolean).map(esc);
			const meta = [
				d.course ? esc(__(d.course)) : "",
				d.hold && !sent ? __("Held") : "",
				flt(d.billed_qty) ? __("Billed {0}", [fmt_qty(d.billed_qty)]) : "",
				flt(d.line_discount_percentage) >= 100 ? `<span class="rf-ok">${__("Free of charge")}</span>` : flt(d.line_discount_percentage) ? `<span class="rf-ok">${__("Discount {0}%", [flt(d.line_discount_percentage, 2)])}</span>` : "",
				d.notes ? `✎ ${esc(d.notes)}` : "",
			].filter(Boolean);
			const slug = (d.kitchen_status || "").replace(/ /g, "");
			const tag = d._pending
				? `<span class="rf-tag t-saving">${ICON.spin}${__("Saving…")}</span>`
				: !sent
				? `<span class="rf-tag t-new">${__("New", null, "Resto")}</span>`
				: `<span class="rf-tag t-${slug}">${in_kitchen(d) || __(d.kitchen_status, null, "Resto")}</span>`;
			const tools = voided
				? ""
				: !sent
				? `<div class="rf-stp"><button data-q="1" aria-label="${__("More")}">${ICON.plus}</button><span>${fmt_qty(d.qty)}</span>
						<button data-q="-1" aria-label="${__("Less")}">${ICON.minus}</button></div>
					<button class="rf-ib rf-del" data-q="trash" title="${__("Remove", null, "Resto")}" aria-label="${__("Remove", null, "Resto")}">${ICON.trash}</button>
					<button class="rf-ib" data-lm="1" title="${__("More")}" aria-label="${__("More")}">${ICON.more}</button>`
				: `${manager ? "" : `<span class="rf-hint rf-hide-sm">${__("Voiding needs a manager's approval")}</span>`}
					<button class="rf-ib" data-lm="1" title="${__("More")}" aria-label="${__("More")}">${ICON.more}</button>`;
			return `<div class="rf-ln ${!sent ? "fresh" : ""} ${voided ? "void" : ""} ${d._pending ? "pending" : ""}" data-line="${esc(d.name)}" data-item="${esc(d.item_code)}">
				<div class="rf-ln1"><div class="rf-lnm"><b>${esc(d.item_name)}</b>
					<small>${o.staff_meal ? fmt_qty(d.qty) : `${fmt_qty(d.qty)} × ${money(each)}`}${extra.length ? ` · ${extra.join(" · ")}` : ""}</small>
					${meta.length ? `<small>${meta.join(" · ")}</small>` : ""}</div>
					${o.staff_meal ? "" : `<b class="rf-lamt">${money(d.amount)}</b>`}</div>
				<div class="rf-ln2">${tag}<span class="rf-spacer"></span>${tools}</div></div>`;
		});
		const free = (o.staff_items || []).map(
			(r) => `<div class="rf-ln fresh"><div class="rf-ln1"><div class="rf-lnm"><b>${esc(r.item_name || r.item_code)}</b>
				<small>${esc(fmt_stock_qty(r.qty))} ${esc(__(r.uom || ""))} · ${__("Free ingredient")}</small></div></div>
				<div class="rf-ln2"><span class="rf-spacer"></span><button class="rf-ib rf-del" data-sfree="${esc(r.item_code)}" title="${__("Remove", null, "Resto")}" aria-label="${__("Remove", null, "Resto")}">${ICON.trash}</button></div></div>`
		);
		this.$order.find(".rf-lines").html([...lines, ...free].join("") || `<p class="rf-empty">${__("Tap menu items to add them.")}</p>`);
		this.$order.find("[data-sfree]").on("click", (e) => this.remove_staff_item($(e.currentTarget).attr("data-sfree")));
		this.$order.find("[data-q]").on("click", (e) => {
			const $l = $(e.currentTarget).closest(".rf-ln");
			const line = o.items.find((d) => d.name === $l.attr("data-line"));
			if (!line) return;
			const q = $(e.currentTarget).attr("data-q");
			if (q === "trash" && flt(line.qty) > 1 && !e.rf_ok) {
				// Asked again on the line as it is then (the lines may have been drawn again meanwhile).
				return frappe.confirm(__("Remove {0} × {1}?", [fmt_qty(line.qty), esc(line.item_name)]), () =>
					this.$order.find(`.rf-ln[data-line="${CSS.escape(line.name)}"] [data-q=trash]`).trigger($.Event("click", { rf_ok: true }))
				);
			}
			if (this._printing) return this.printing_wait();
			const step = q === "trash" ? -flt(line.qty) : parseInt(q);
			if (line._pending) return this.step_pending(line, step); // no line on the server yet
			if (step > 0 && !this.floor.can_order) return frappe.msgprint(esc(this.floor.no_order_reason));
			this._gen++;
			const qty = flt(line.qty) + step;
			const each = flt(line.qty) ? flt(line.amount) / flt(line.qty) : flt(line.rate);
			o.net_total = flt(o.net_total) + step * each;
			if (qty <= 0) o.items = o.items.filter((d) => d !== line);
			else {
				line.qty = qty;
				line.amount = each * qty;
			}
			this.render_lines();
			this.flush_taps();
			// The dish named, so a change kept on the device says which line it is.
			if (q === "trash") this.sync_call("remove_line", { order: o.name, line: line.name }, `${__("a line removed")}: ${line.item_name}`);
			else this.sync_call("update_line", { order: o.name, line: line.name, step }, `${step > 0 ? "+" : "−"}${fmt_qty(Math.abs(step))} × ${line.item_name}`);
		});
		this.$order.find("[data-lm]").on("click", (e) => {
			const line = o.items.find((d) => d.name === $(e.currentTarget).closest(".rf-ln").attr("data-line"));
			if (!line) return;
			if (line._pending) return frappe.show_alert({ message: __("Being saved: try again in a moment"), indicator: "blue" });
			this.line_menu(line);
		});

		// Totals. Before the bill is asked for, the total is the dishes less the discount: the bill
		// adds the service charge, the delivery fee and the tax (when prices do not include it).
		const live = o.items.filter((d) => d.kitchen_status !== "Void");
		const count = live.reduce((s, d) => s + flt(d.qty), 0);
		this.$order.find(".rf-ccount").text(__("{0} items", [fmt_qty(count)]));
		const disc = flt(o.discount_amount);
		const total = o.bill_grand_total ? flt(o.bill_grand_total) : flt(o.net_total) - disc;
		const added = !o.bill_grand_total && (flt(o.service_charge_percent) || this.floor.tax_added || (o.order_type === "Delivery" && flt(o.delivery_fee)));
		if (o.staff_meal) {
			const n = o.items.filter((d) => d.kitchen_status !== "Void").reduce((s, d) => s + flt(d.qty), 0);
			this.$order.find(".rf-ccount").text(__("{0} items", [fmt_qty(n)]));
			this.$order.find(".rf-totals").html(`<small class="rf-muted">${__("Taken from the kitchen's stock at cost when sent: no bill")}</small>`);
			this.$order.find(".rf-onote").remove();
			if (o.notes) this.$order.find(".rf-lines").prepend(`<div class="rf-onote">✎ ${esc(o.notes)}</div>`);
			this.update_badges();
			return;
		}
		this.$order.find(".rf-totals").html(`
			<div class="rf-trow"><span>${__("Items Total")}</span><span class="rf-spacer"></span><span>${money(o.net_total)}</span></div>
			${disc ? `<div class="rf-trow"><span>${__("Discount")}</span><span class="rf-spacer"></span><span dir="ltr">−${money(disc)}</span></div>` : ""}
			${flt(o.service_charge_percent) ? `<div class="rf-trow rf-muted"><span>${__("Service Charge")} ${o.service_charge_percent}%</span></div>` : ""}
			${o.invoiced_total ? `<div class="rf-trow rf-muted"><span>${__("Paid So Far")}</span><span class="rf-spacer"></span><span>${money(o.invoiced_total)}</span></div>` : ""}
			${this.packaging_row(o)}
			<div class="rf-grand"><b>${o.bill_grand_total ? __("Bill Total") : __("Total", null, "Resto")}</b><span class="rf-spacer"></span><b class="rf-gv">${money(total)}</b></div>
			${added ? `<small class="rf-muted">${__("The service charge, delivery fee and tax are added on the bill")}</small>` : ""}`);
		const unsent = o.items.filter((d) => d.kitchen_status === "Not Sent" && !d.hold).length;
		const held = o.items.filter((d) => d.kitchen_status === "Not Sent" && d.hold).length;
		// The buttons stay; only their text changes (a tap on them is never lost to a redraw).
		// Nothing new for the kitchen: the button says it is done and rests until a line is added
		// or changed (a dish tapped shows at once as not sent, so it is ready again at once).
		const sent = o.items.some((d) => !["Not Sent", "Void"].includes(d.kitchen_status));
		this.$order
			.find("[data-act=send]")
			.prop("disabled", !unsent)
			.toggleClass("rf-btn-d", !!unsent)
			.toggleClass("rf-done", !unsent && sent);
		this.$order.find(".rf-send-t").text(unsent ? `${__("Send to Kitchen")} (${unsent})` : sent ? `${__("Sent to Kitchen")} ✓` : __("Send to Kitchen"));
		this.$order.find(".rf-fire").toggle(!!held).text(__("Fire Held Dishes ({0})", [held]));
		this.$order.find(".rf-pay-b").text(`${__("Pay", null, "Resto")}${added || flt(o.invoiced_total) ? "" : ` · ${money(total)}`}`);
		this.$order.find(".rf-onote").remove();
		if (o.notes) this.$order.find(".rf-lines").prepend(`<div class="rf-onote">✎ ${esc(o.notes)}</div>`);
		this.render_customer();
		this.update_badges();
		// The dish just tapped is shown, even at the end of a long order.
		if (this._show_code) {
			const el = this.$order.find(`.rf-ln[data-item="${CSS.escape(this._show_code)}"]`).last()[0];
			this._show_code = null;
			if (el) el.scrollIntoView({ block: "nearest" });
		}
	}

	// Consumables the cashier adds to a big order (D-217, D-224): one tap for one more bag, of the
	// rows ticked for the order's type. They are taken from stock on the bill, at no price.
	packaging_row(o) {
		const flag = { "Dine In": "dine_in", Takeaway: "takeaway", Delivery: "delivery" }[o.order_type];
		const add = (this.floor.packaging_addable || []).filter((p) => flag && p[flag]);
		const billed = (o.invoices || []).some((d) => !d.cancelled);
		if (!add.length || billed) return "";
		let extra = {};
		try {
			extra = JSON.parse(o.extra_packaging || "{}") || {};
		} catch (e) {
			extra = {};
		}
		return `<div class="rf-pack"><span class="rf-muted">${__("Extra Packaging")}</span>${add
			.map((p) => {
				const n = cint(extra[p.item_code]);
				const name = esc(p.item_name);
				return `<span class="rf-packi">${
					n ? `<button class="rf-ib rf-sm" data-pack="${esc(p.item_code)}" data-n="${n - 1}" title="${__("One Less")}" aria-label="${__("One Less")}">−</button>` : ""
				}<button class="rf-chip ${n ? "on" : ""}" data-pack="${esc(p.item_code)}" data-n="${n + 1}">+ ${name}${n ? ` ×${n}` : ""}</button></span>`;
			})
			.join("")}</div>`;
	}

	async set_pack(item_code, qty) {
		const o = this.order;
		if (!o || this._packing) return;
		this._packing = true;
		try {
			if (!(await this.settled())) return;
			await call("set_extra_packaging", { order: o.name, item_code, qty }, true);
			if (this.order && this.order.name === o.name) await this.load_order(o.name);
		} catch (e) {
			// the server said why (shown already)
		} finally {
			this._packing = false;
		}
	}

	// + on a line just tapped is one more tap; − (or the bin) takes back taps not sent yet.
	step_pending(line, step) {
		if (step > 0) return this.tap_item({ item_code: line.item_code, item_name: line.item_name, rate: line.rate });
		const taps = this._taps || [];
		const n = Math.round(-step);
		if (taps.filter((c) => c === line.item_code).length < n) {
			return frappe.show_alert({ message: __("Being saved: try again in a moment"), indicator: "blue" });
		}
		for (let k = 0; k < n; k++) taps.splice(taps.lastIndexOf(line.item_code), 1);
		this._gen++;
		const o = this.order;
		line.qty = flt(line.qty) - n;
		line.amount = flt(line.amount) - n * flt(line.rate);
		o.net_total = flt(o.net_total) - n * flt(line.rate);
		if (line.qty <= 0) o.items = o.items.filter((d) => d !== line);
		this.render_lines();
	}

	async line_call(method, args) {
		if (!(await this.settled())) return;
		const name = this.order.name;
		await call(method, { order: name, ...args });
		if (this.order && this.order.name === name) await this.reload_lines(name);
	}

	// A line's extra portions free or charged again (D-253). The server checks the limit and the PIN.
	async free_extras(line, free) {
		if (!(await this.settled())) return;
		const name = this.order.name;
		const send = (ok) =>
			new Promise((resolve, reject) =>
				frappe.call({
					method: "resto.resto.floor.set_extras_free",
					args: { order: name, line: line.name, free: free ? 1 : 0, ...ok },
					callback: (r) => resolve(r.message),
					error: (r) => reject(r || {}),
				})
			);
		let done = null;
		try {
			done = await send({});
		} catch (r) {
			if (r.exc_type !== "ApprovalRequired" || this.floor.roles.manager) return;
			const ok = await ask_approval(this.floor, __("Free extra portions: {0} × {1}", [fmt_qty(line.qty), line.item_name]));
			if (!ok) return;
			done = await send(ok).catch(() => null);
		}
		if (done && this.order && this.order.name === name) await this.reload_lines(name);
	}

	// A discount on one line (D-271): % or an amount for the whole line, with a reason; 100% makes
	// the dish free (its stock is issued all the same). Above the user's discount limit a manager's PIN.
	async line_discount(line) {
		if (!(await this.settled())) return;
		const name = this.order.name;
		const full = flt(line.qty) * (flt(line.rate) + flt(line.modifiers_amount));
		const v = await new Promise((resolve) => {
			let picked = null;
			const d = new frappe.ui.Dialog({
				title: `${__("Item Discount")}: ${esc(line.item_name)}`,
				fields: [
					{ fieldtype: "HTML", options: `<p class="text-muted">${__("Price of the line: {0}", [money(full)])}</p>` },
					{ fieldname: "percentage", fieldtype: "Percent", label: __("Discount %"), default: flt(line.line_discount_percentage) },
					{ fieldname: "amount", fieldtype: "Currency", label: __("Or Discount Amount (whole line)") },
					{ fieldname: "reason", fieldtype: "Data", label: __("Discount Reason"), default: line.line_discount_reason || "" },
				],
				primary_action_label: __("Save"),
				primary_action: (vals) => {
					picked = vals;
					d.hide();
				},
				secondary_action_label: __("Free (100%)"),
				secondary_action: () => {
					picked = { ...d.get_values(true), percentage: 100, amount: 0 };
					d.hide();
				},
			});
			d.onhide = () => resolve(picked);
			d.show();
		});
		if (!v) return;
		// An amount typed wins over the % already shown (the line's current discount).
		const percent = full && flt(v.amount) ? (flt(v.amount) / full) * 100 : flt(v.percentage);
		let ok = {};
		if (percent > flt(line.line_discount_percentage) + 1e-6 && percent > flt(this.floor.discount_limit) + 1e-6) {
			ok = await ask_approval(this.floor, __("Item discount of {0}% on {1}", [flt(percent, 2), line.item_name]));
			if (!ok) return;
		}
		await call("set_line_discount", { order: name, line: line.name, percentage: flt(percent, 6), reason: v.reason || "", ...ok }, true);
		if (this.order && this.order.name === name) await this.reload_lines(name);
	}

	line_menu(line) {
		const sent = line.kitchen_status !== "Not Sent";
		if (line.kitchen_status === "Void") return;
		const fields = [];
		if (!sent) {
			fields.push(
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Kitchen Note"), default: line.notes },
				{ fieldname: "course", fieldtype: "Select", options: COURSES.map((c) => ({ value: c, label: __(c) })), label: __("Course", null, "Resto"), default: line.course || "" },
				{ fieldname: "hold", fieldtype: "Check", label: __("Hold Until Fired"), default: line.hold }
			);
		} else {
			fields.push(
				{ fieldname: "reason", fieldtype: "Data", label: __("Void Reason"), reqd: 1 },
				{
					fieldname: "cooked",
					fieldtype: "Check",
					label: __("Already Cooked (post its ingredients as waste)"),
					default: ["In Progress", "Ready", "Served"].includes(line.kitchen_status) ? 1 : 0,
				}
			);
		}
		// Its extra portions given free, or charged again (D-253): a cashier, within the limit or with a PIN.
		const can_free = (this.floor.roles.cashier || this.floor.roles.manager) && !flt(line.billed_qty) && (line.can_free_extras || line.sides_free);
		const can_discount =
			(this.floor.roles.cashier || this.floor.roles.manager) && !flt(line.billed_qty) && !this.order.staff_meal && flt(line.rate) + flt(line.modifiers_amount) > 0;
		const can_sides = !sent && this.order.sides_enabled && line.has_sides;
		// The line's actions (D-275): real buttons, together at the top, each with its icon and colour;
		// filled when it is in force (a discount given, extras given free).
		const pct = flt(line.line_discount_percentage);
		const acts = [
			can_discount && ["disc", "rf-la-disc", pct, ICON.tag, pct >= 100 ? __("Free of charge") : pct ? __("Item Discount {0}%", [flt(pct, 2)]) : __("Item Discount")],
			can_free && ["free", "rf-la-free", line.sides_free, ICON.gift, line.sides_free ? __("Charge the Extra Again") : __("Free Extra")],
			can_sides && ["sides", "rf-la-sides", 0, ICON.swap, __("Change Sides")],
		].filter(Boolean);
		if (acts.length) fields.unshift({ fieldname: "acts", fieldtype: "HTML" });
		const d = new frappe.ui.Dialog({
			title: esc(line.item_name),
			fields,
			primary_action_label: sent ? __("Void Item") : __("Save"),
			primary_action: async (v) => {
				d.hide();
				if (sent) {
					const ok = await ask_approval(this.floor, `${__("Void an item sent to the kitchen")}: ${line.qty} × ${line.item_name}`);
					if (ok) await this.line_call("void_line", { line: line.name, reason: v.reason, cooked: v.cooked ? 1 : 0, ...ok });
				}
				else await this.line_call("update_line", { line: line.name, notes: v.notes || "", course: v.course || "", hold: v.hold ? 1 : 0 });
			},
		});
		if (acts.length) {
			const run = {
				disc: () => {
					d.hide();
					this.line_discount(line);
				},
				free: () => {
					d.hide();
					this.free_extras(line, !line.sides_free);
				},
				// Sides can be changed (Restaurant Settings) and this dish has some. What was typed in
				// this dialog is saved first, so a kitchen note is not lost.
				sides: async () => {
					const v = d.get_values(true) || {};
					d.hide();
					await call("update_line", { order: this.order.name, line: line.name, notes: v.notes || "", course: v.course || "", hold: v.hold ? 1 : 0 });
					this.sides_dialog([line.name]);
				},
			};
			d.fields_dict.acts.$wrapper.html(
				`<div class="rf-root rf-mdlg rf-line-acts">${acts
					.map(([k, cls, on, icon, label]) => `<button type="button" class="rf-btn rf-la ${cls}${on ? " rf-la-on" : ""}" data-la="${k}">${icon}<span>${label}</span></button>`)
					.join("")}</div>`
			);
			d.fields_dict.acts.$wrapper.find("[data-la]").on("click", (e) => run[e.currentTarget.dataset.la]());
		}
		if (!sent) d.set_secondary_action_label(__("Remove", null, "Resto"));
		if (!sent) d.set_secondary_action(() => { d.hide(); this.line_call("remove_line", { line: line.name }); });
		d.show();
	}

	async action(act, e) {
		if (this._acting && Date.now() - this._acting < 30000) return; // one action at a time
		this._acting = Date.now();
		this.$order.addClass("rf-acting");
		try {
			// The kitchen ticket and the bill never wait for the connection (D-219).
			if (act === "send" || act === "bill") return await this.send_or_bill(act, e);
			if (!(await this.settled())) return;
			return await this.do_action(act, e);
		} finally {
			this._acting = 0;
			this.$order.removeClass("rf-acting");
		}
	}

	// Send to Kitchen and Print Bill (D-219): through the server when it answers and nothing waits on
	// this device; otherwise printed here, from the order as this device has it, so a guest never
	// waits for the connection to eat or to leave.
	async send_or_bill(act, e) {
		// Connected: what waits is sent first. Offline: the taps are kept with the rest, and nothing
		// is asked now (the paper comes first).
		await this.flush_taps(!this.offline);
		// The send's id: the same send printed here after its answer was lost is not recorded twice.
		const op = act === "send" ? frappe.utils.get_random(20) : null;
		if (act === "send") this._printing = true; // what is sent and what is on paper stay the same
		try {
			let tried = false;
			if (!outbox().length && !this.offline) {
				try {
					return await this.do_action(act, e, op);
				} catch (err) {
					if (!(err && err.network)) throw err;
					tried = true;
					if (act === "send" && (await this.sent_meanwhile())) return;
				}
			}
			return act === "send" ? await this.send_here(op, tried) : await this.bill_here();
		} finally {
			if (act === "send") this._printing = false;
		}
	}

	// The send's answer was lost (a time limit, the connection dropping): it may have reached the
	// kitchen. A short look first, so the kitchen does not get the same dishes twice.
	async sent_meanwhile() {
		const o = this.order;
		const unsent = o.items.filter((d) => d.kitchen_status === "Not Sent" && !d.hold && !d._pending);
		if (!unsent.length) return false;
		try {
			const fresh = await call("get_order", { order: o.name }, false, 5000);
			const still = new Set((fresh.items || []).filter((d) => d.kitchen_status === "Not Sent").map((d) => d.name));
			if (unsent.some((d) => still.has(d.name))) return false;
			this.mark_sent(unsent);
		} catch (e) {
			return false;
		}
		frappe.show_alert({ message: __("Sent to the kitchen"), indicator: "green" });
		this.reload_quietly(o.name);
		return true;
	}

	// Lines the server took for the kitchen, shown so until the order is read again (a second press
	// while the connection is down must not print them again).
	mark_sent(lines) {
		lines.forEach((d) => (d.kitchen_status = "Sent"));
		this._gen++;
		this.render_lines();
	}

	// The order read again after a change the server took; a failure here changes nothing (the next
	// refresh draws it).
	reload_quietly(name) {
		return Promise.resolve()
			.then(() => this.load_order(name))
			.catch(() => {});
	}

	printing_wait() {
		frappe.show_alert({ message: __("Sending to the kitchen: add it in a moment."), indicator: "orange" });
	}

	// The kitchen ticket printed on this device. The server records it when the connection is back,
	// for these dishes only and without printing it again (send_to_kitchen with printed).
	async send_here(op, tried) {
		const o = this.order;
		const lines = o.items.filter((d) => d.kitchen_status === "Not Sent" && !d.hold);
		if (!lines.length) return frappe.show_alert({ message: __("Nothing new to send"), indicator: "blue" });
		// A device that does not print (a waiter's tablet): nothing is marked sent, the kitchen gets nothing.
		if (!this.prints_here()) {
			return frappe.msgprint(__("No connection, and this device does not print. The dishes stay unsent: send them again once the connection is back, or print from the cashier's device."));
		}
		const printed = {};
		lines.forEach((d) => (printed[d.item_code] = flt(printed[d.item_code]) + flt(d.qty)));
		const printed_at = frappe.datetime.now_datetime();
		// The lines the device had from the server, matched first by the server.
		const names = lines.filter((d) => d.name && !d._pending).map((d) => d.name);
		const ok = await resto.print_html(this.ticket_html(o, lines, tried));
		if (!ok) {
			return frappe.msgprint(__("No connection, and this device could not print the kitchen ticket. Try again, or tell the kitchen."));
		}
		// The dishes went to the kitchen on paper: the changes that made them are sent, never
		// discarded (the order must bill what was cooked).
		set_outbox(outbox().map((j) => (j && j.args && j.args.order === o.name ? { ...j, ok: 1, kept: 1, args: { ...j.args, kitchen_printed: 1 } } : j)));
		const what = lines.map((d) => `${fmt_qty(d.qty)} × ${d.item_name}`).join("، ");
		lines.forEach((d) => (d.kitchen_status = "Sent")); // as shown until the server answers
		this._gen++;
		this.render_lines();
		frappe.show_alert({ message: __("No connection: the kitchen ticket was printed here. Hand it to the kitchen; it is recorded when the connection is back."), indicator: "orange" }, 10);
		this.sync_call("send_to_kitchen", { order: o.name, printed, printed_at, lines: names }, __("Kitchen ticket printed on this device: {0}", [what]), { ok: 1, kept: 1, ...(op ? { op } : {}) });
	}

	// The bill printed on this device, its total worked out here as the server does (Resto Settings,
	// the POS Profile's taxes and rounding, read with the floor). The table is not marked "bill
	// requested" until the next bill through the server.
	async bill_here() {
		const o = this.order;
		if (!this.prints_here()) {
			return frappe.msgprint(__("No connection, and this device does not print bills. Print the bill from the cashier's device."));
		}
		// As the server's bill: what is not sent to the kitchen is sent or taken off first.
		const unsent = o.items.filter((d) => d.kitchen_status === "Not Sent");
		if (unsent.length) {
			return frappe.msgprint(__("Send these items to the kitchen or remove them first: {0}", [esc(unsent.map((d) => d.item_name).join(", "))]));
		}
		const bill = this.bill_on_device(o);
		if (!bill) return frappe.msgprint(__("Nothing left to bill on this order"));
		if (!(await resto.print_html(this.bill_html(o, bill)))) {
			return frappe.msgprint(__("No connection, and this device could not print the bill. Try again."));
		}
		frappe.show_alert({ message: __("No connection: the bill was printed on this device."), indicator: "orange" }, 8);
	}

	// The bill of what is not billed yet, as billing.build_invoice works it out: food (with options
	// and sides), the order discount on it, the delivery fee, the service charge as a tax row (first,
	// and taxed, with "Charge VAT on Service"), the template's taxes, then rounding. estimate: part
	// of the order is paid already, or the rules were not read, so the total may differ.
	bill_on_device(o) {
		const rules = (this.floor && this.floor.bill_rules) || null;
		const r = rules || {};
		const round2 = (v) => sys_round(v, 2, r.rounding_method);
		const rows = o.items
			.filter((d) => d.kitchen_status !== "Void" && flt(d.qty) - flt(d.billed_qty) > 1e-9)
			.map((d) => {
				const qty = flt(d.qty) - flt(d.billed_qty);
				const each = flt(d.qty) ? flt(d.amount) / flt(d.qty) : flt(d.rate);
				return { name: d.item_name, text: d.modifiers_text || "", qty, amount: round2(qty * each) };
			});
		if (!rows.length) return null;
		const food = round2(rows.reduce((s, x) => s + x.amount, 0));
		const partial = flt(o.invoiced_total) > 0;
		// A percentage discount is of the food now on the order (dishes may have been added on this device).
		const given = flt(o.discount_percentage)
			? (flt(o.discount_percentage) * food) / 100
			: partial
			? (flt(o.discount_amount) * food) / (flt(o.net_total) || food)
			: flt(o.discount_amount);
		const discount = round2(Math.min(food, given));
		const fee = o.order_type === "Delivery" && !partial ? flt(o.delivery_fee) : 0;
		const pct = flt(o.service_charge_percent);
		const net = round2(food + fee - discount);
		let taxes = (r.taxes || []).map((t) => ({ ...t }));
		const inclusive = taxes.some((t) => cint(t.included_in_print_rate));
		const is_service = (t) => t.service || (r.service_account && t.account_head === r.service_account);
		const fill = (t) => {
			t.included_in_print_rate = 0;
			if (fee) {
				t.charge_type = "Actual";
				t.tax_amount = round2(((food - discount) * pct) / 100);
			} else {
				t.charge_type = "On Net Total";
				t.rate = pct;
			}
		};
		const service_rows = taxes.filter(is_service);
		if (service_rows.length) service_rows.forEach(fill);
		else if (pct && !inclusive) {
			const row = { description: __("Service Charge"), service: 1 };
			fill(row);
			if (cint(r.vat_on_service)) {
				taxes.forEach((t) => {
					if (t.charge_type === "On Net Total") {
						t.charge_type = "On Previous Row Total";
						t.row_id = "1";
					} else if (["On Previous Row Amount", "On Previous Row Total"].includes(t.charge_type) && t.row_id) {
						t.row_id = String(cint(t.row_id) + 1);
					}
				});
				taxes = [row, ...taxes];
			} else taxes.push(row);
		}
		const out = [];
		let total = net;
		if (!inclusive) {
			taxes.forEach((t) => {
				const before = out[cint(t.row_id) - 1];
				let amount = 0;
				if (t.charge_type === "Actual") amount = flt(t.tax_amount);
				else if (t.charge_type === "On Net Total") amount = (net * flt(t.rate)) / 100;
				else if (t.charge_type === "On Previous Row Amount") amount = ((before ? before.amount : 0) * flt(t.rate)) / 100;
				else if (t.charge_type === "On Previous Row Total") amount = ((before ? before.total : net) * flt(t.rate)) / 100;
				amount = round2(amount);
				total = round2(total + amount);
				out.push({ description: t.description, amount, total, service: !!is_service(t) });
			});
		}
		const payable = cint(r.disable_rounded_total) ? total : round_money(total, flt(r.smallest_fraction), r.rounding_method);
		return { rows, food, discount, fee, taxes: out.filter((x) => x.amount), grand: total, payable, estimate: partial || !rules || r.exact === 0 };
	}

	paper(title, body) {
		const dir = frappe.utils.is_rtl && frappe.utils.is_rtl() ? "rtl" : document.dir || "rtl";
		return `<!doctype html><html dir="${dir}"><head><meta charset="utf-8"><title>${esc(title)}</title><style>
			@page { margin: 0; } * { box-sizing: border-box; }
			body { margin: 0; padding: 4mm; width: 72mm; font-family: Almarai, Tahoma, Arial, sans-serif; font-size: 12px; color: #000; direction: ${dir}; }
			h1 { font-size: 15px; margin: 0 0 2mm; text-align: center; } h2 { font-size: 13px; margin: 0 0 2mm; text-align: center; border: 1px solid #000; padding: 1mm; }
			.m { display: flex; justify-content: space-between; gap: 2mm; } .s { font-size: 11px; } hr { border: 0; border-top: 1px dashed #000; margin: 2mm 0; }
			.big { font-size: 15px; font-weight: 700; } .k { font-size: 16px; font-weight: 700; margin-top: 2mm; } .n { font-size: 12px; padding-inline-start: 4mm; } .n .w { color: #c00; font-weight: 800; font-size: 15px; }
		</style></head><body>${body}</body></html>`;
	}

	paper_head(o) {
		const where = o.resto_table ? `${__("Table", null, "Resto")} ${esc(o.resto_table)}` : esc(__(o.order_type, null, "Resto"));
		return `${o.order_no ? `<div class="big" style="text-align:center;font-size:22px">${__("Order {0}", [o.order_no], "Resto")}</div>` : ""}
			<div class="m s"><span>${esc(o.name)}</span><span>${where}</span></div>
			<div class="m s"><span>${esc(frappe.datetime.str_to_user(frappe.datetime.now_datetime()))}</span><span>${esc((this.floor && this.floor.user_name) || frappe.session.user)}</span></div>`;
	}

	ticket_html(o, lines, tried) {
		const rows = lines
			.map((d) => {
				const notes = [d.modifiers_text, d.sides_text || d.extras_text, d.notes, d.course ? __(d.course) : ""].filter(Boolean);
				// The «without» part of the sides stands out, as on the kitchen's own paper (D-251).
				return `<div class="k">${fmt_qty(d.qty)} × ${esc(d.item_name)}</div>${notes.map((n) => `<div class="n">${n === d.sides_text ? sides_paper(n) : esc(n)}</div>`).join("")}`;
			})
			.join("");
		return this.paper(
			__("Kitchen Ticket"),
			`<h2>${__("Kitchen ticket, printed without a connection")}</h2>${this.paper_head(o)}${cint(o.send_rounds) ? `<div class="big" style="text-align:center">*** ${__("Addition {0}", [cint(o.send_rounds) + 1])} ***</div>` : ""}${tried ? `<h2>${__("Sent first without an answer: check with the kitchen that these are not there twice.")}</h2>` : ""}<hr>${rows}<hr><div class="s">${__("Recorded in the system when the connection is back.")}</div>`
		);
	}

	bill_html(o, b) {
		const line = (label, value, cls) => `<div class="m ${cls || ""}"><span>${label}</span><span dir="ltr">${value}</span></div>`;
		const rows = b.rows.map((x) => line(`${fmt_qty(x.qty)} × ${esc(x.name)}${x.text ? ` <span class="s">(${esc(x.text)})</span>` : ""}`, money(x.amount))).join("");
		const brand = (this.floor && this.floor.brand) || "";
		return this.paper(
			__("Bill"),
			`${brand ? `<h1>${esc(brand)}</h1>` : ""}<h2>${__("Bill, printed without a connection")}</h2>${this.paper_head(o)}<hr>${rows}<hr>
			${line(__("Items Total"), money(b.food))}
			${b.discount ? line(__("Discount"), `−${money(b.discount)}`) : ""}
			${b.fee ? line(__("Delivery Fee"), money(b.fee)) : ""}
			${b.taxes.map((t) => line(esc(t.service ? __("Service Charge") : t.description), money(t.amount))).join("")}
			${b.payable !== b.grand ? line(__("Rounding"), money(b.payable - b.grand)) : ""}
			<hr>${line(__("Total", null, "Resto"), money(b.payable), "big")}
			<hr><div class="s">${b.estimate ? __("An estimate: the final bill is made when it is paid.") : __("The bill is checked again when it is paid.")}</div>`
		);
	}

	async do_action(act, e, op) {
		const o = this.order;
		if (act === "send") {
			const unsent = o.items.filter((d) => d.kitchen_status === "Not Sent" && !d.hold);
			const tickets = await call("send_to_kitchen", { order: o.name, op }, true, 15000);
			if (tickets.length) this.mark_sent(unsent);
			frappe.show_alert({ message: tickets.length ? __("Sent to {0} station(s)", [tickets.length]) : __("Nothing new to send"), indicator: tickets.length ? "green" : "blue" });
			// Sent: a reload that fails now is not a send to make again (the next refresh draws it).
			return this.reload_quietly(o.name);
		}
		if (act === "fire") {
			const held = [...new Set(o.items.filter((d) => d.hold && d.kitchen_status === "Not Sent").map((d) => d.course || ""))];
			if (!held.length) return frappe.show_alert({ message: __("No held courses"), indicator: "blue" });
			const d = new frappe.ui.Dialog({
				title: __("Fire Course"),
				fields: [{ fieldname: "course", fieldtype: "Select", options: held.map((c) => ({ value: c, label: c ? __(c) : __("No Course") })), default: held[0] }],
				primary_action_label: __("Fire"),
				primary_action: async (v) => { d.hide(); await call("fire_course", { order: o.name, course: v.course }, true); this.load_order(o.name); },
			});
			return d.show();
		}
		if (act === "served") {
			await call("mark_served", { order: o.name });
			return this.load_order(o.name);
		}
		if (act === "bill") {
			if (this._billing) return;
			this._billing = true;
			try {
				const bill = await call("request_bill", { order: o.name }, true, 15000);
				if (bill && bill.on_printer) frappe.show_alert({ message: __("Sent to printer"), indicator: "green" });
				else if (bill && bill.print_error) frappe.show_alert({ message: __("Bill requested, but it could not be printed. Tell the manager."), indicator: "red" }, 10);
				else if (bill && bill.pages && bill.pages.length && this.prints_here()) {
					frappe.show_alert({ message: __("Printing the bill…"), indicator: "blue" });
					await resto.print_pages(bill.pages);
				} else if (bill && bill.pages && bill.pages.length) {
					frappe.show_alert({ message: __("Bill requested. Print it from the cashier device (Print Bill there)."), indicator: "orange" }, 8);
				} else frappe.show_alert({ message: __("Bill requested"), indicator: "green" });
			} finally {
				this._billing = false;
			}
			return this.reload_quietly(o.name);
		}
		if (act === "pay") return this.pay();
		if (act === "staff_post") return this.post_staff_meal();
		if (act === "staff_item") return this.staff_item();
		if (act === "more") return this.more_menu(e);
		if (act === "note") return this.edit_info(false, "notes");
		if (act === "discount") return this.discount();
		if (act === "customer") return this.edit_info();
		if (act === "new_customer") return this.edit_info(true);
	}

	more_menu() {
		const o = this.order;
		if (o.staff_meal) {
			// A staff meal: no bill, customer, table or course firing.
			const staff = [[__("Note"), () => this.action("note")]];
			const many = o.sides_enabled ? o.items.filter((d) => d.kitchen_status === "Not Sent" && d.has_sides) : [];
			if (many.length > 1 || (many.length === 1 && flt(many[0].qty) > 1)) staff.push([__("Sides for Several Dishes"), () => this.pick_side_lines(many)]);
			staff.push([__("Open Full Form"), () => frappe.set_route("Form", "Resto Order", o.name)], [__("Cancel Order"), () => this.cancel(), "rf-danger"]);
			return this.menu_dialog(__("Order Actions"), staff);
		}
		const acts = [
			[__("Fire Course"), () => this.action("fire")],
			[__("Served"), () => this.action("served")],
			[__("Discount"), () => this.action("discount")],
			[__("Guests and Customer"), () => this.action("customer")],
		];
		const with_sides = o.sides_enabled ? o.items.filter((d) => d.kitchen_status === "Not Sent" && d.has_sides) : [];
		if (with_sides.length > 1 || (with_sides.length === 1 && flt(with_sides[0].qty) > 1)) {
			acts.push([__("Sides for Several Dishes"), () => this.pick_side_lines(with_sides)]);
		}
		if (o.print_sides || o.items.some((d) => d.sides_text && d.kitchen_status !== "Void")) {
			// One entry that shows its state: ☑ printed, ☐ not printed.
			acts.push([
				`${o.print_sides ? "☑" : "☐"} ${__("Print Sides on the Receipt")}`,
				async () => {
					await call("set_print_sides", { order: o.name, print_sides: o.print_sides ? 0 : 1 }, true);
					frappe.show_alert({ message: o.print_sides ? __("Sides will not be printed on the receipt") : __("Sides will be printed on the receipt"), indicator: "green" });
					this.load_order(o.name);
				},
			]);
		}
		if (o.order_type === "Dine In") {
			acts.push([__("Move to Table"), () => this.move()], [__("Merge Table Into This"), () => this.merge()]);
		}
		if (o.order_type === "Delivery") acts.push([__("Delivery Status"), () => this.delivery()]);
		if (this.floor.roles.manager) acts.push([__("Service Charge"), () => this.service()]);
		acts.push([__("Open Full Form"), () => frappe.set_route("Form", "Resto Order", o.name)], [__("Cancel Order"), () => this.cancel(), "rf-danger"]);
		this.menu_dialog(__("Order Actions"), acts);
	}

	pick_side_lines(lines) {
		const d = new frappe.ui.Dialog({
			title: __("Sides for Several Dishes"),
			fields: [
				{
					fieldname: "lines",
					fieldtype: "MultiCheck",
					label: __("Dishes"),
					sort_options: false,
					columns: 1,
					options: lines.map((l) => ({
						label: `${esc(l.item_name)} × ${l.qty}${l.modifiers_text ? " (" + esc(l.modifiers_text) + ")" : ""}`,
						value: l.name,
						checked: 1,
					})),
				},
			],
			primary_action_label: __("Next"),
			primary_action: (v) => {
				if (!v.lines || !v.lines.length) return frappe.show_alert({ message: __("Choose at least one dish"), indicator: "orange" });
				d.hide();
				this.sides_dialog(v.lines, true);
			},
		});
		d.show();
	}

	// The sides dialog's fields (D-268). A slot of one portion per dish (a side group of max 1) is a
	// choice of one of its options or «without»; a slot of more portions is a count per option,
	// whole numbers unless a shared choice gives a part of one.
	sides_fields(r, on_change) {
		const fields = [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${r.per_dish ? __("Portions for each dish.") : __("Enter the total for the chosen dishes together.")}</p>`,
			},
		];
		r.slots.forEach((s, si) => {
			const free = s.free ? " · " + __("{0} free", [s.free]) : "";
			fields.push({ fieldtype: "Section Break", label: `${esc(s.label)} · ${__("max {0}", [s.max])}${free}${s.shared ? " · " + __("shared", null, "Resto") : ""}` });
			if (one_of(r, s)) {
				// The option served now (the most portions if a choice from before has more than one).
				const now = s.options.reduce((a, opt, oi) => (flt(opt.total) > (a < 0 ? 0 : flt(s.options[a].total)) ? oi : a), -1);
				const radio = (value, label, on) =>
					`<label class="rf-radio"><input type="radio" name="rfs_${si}" value="${value}" ${on ? "checked" : ""}><span>${label}</span></label>`;
				fields.push({
					fieldname: `r_${si}`,
					fieldtype: "HTML",
					options: `<div class="rf-radios">${s.options
						.map((opt, oi) => radio(oi, esc(opt.item_name) + (opt.price ? ` (+${money(opt.price)} ${__("per extra portion")})` : ""), oi === now))
						.join("")}${radio("", __("Without", null, "Resto"), now < 0)}</div>`,
				});
				return;
			}
			const whole = r.per_dish && !s.shared && s.options.every((opt) => Math.abs(flt(opt.total) - Math.round(flt(opt.total))) < 1e-6);
			s.options.forEach((opt, oi) => {
				if (s.single && s.options.length === 1) {
					// A side without a group: served or not.
					fields.push({ fieldname: `s_${si}_${oi}`, fieldtype: "Check", label: esc(opt.item_name), default: opt.total > 0 ? 1 : 0 });
				} else {
					// An extra portion's price, when portions above the recipe's are charged.
					const price = opt.price ? ` (+${money(opt.price)} ${__("per extra portion")})` : "";
					fields.push({
						fieldname: `s_${si}_${oi}`,
						fieldtype: whole ? "Int" : "Float",
						precision: whole ? undefined : "3",
						label: esc(opt.item_name) + price,
						default: whole ? Math.round(flt(opt.total)) : flt(opt.total, 3),
						non_negative: 1,
						onchange: on_change,
					});
				}
			});
		});
		return fields;
	}

	// The portions now on the dialog: {slot index: [portions per option]}. get(fieldname) reads a
	// field, picked(si) the chosen option of a one-portion slot ("" for without).
	sides_values(r, get, picked) {
		return r.slots.map((s, si) => {
			if (one_of(r, s)) {
				const p = picked(si);
				return s.options.map((opt, oi) => (p !== "" && p !== undefined && p !== null && cint(p) === oi ? 1 : 0));
			}
			return s.options.map((opt, oi) => flt(get(`s_${si}_${oi}`)));
		});
	}

	// The first slot whose portions are above its most (D-269), or null: per dish for one dish, in
	// total for several; a shared choice is limited by its set on the server.
	sides_over(r, values) {
		const over = r.slots.find((s, si) => !s.shared && values[si].reduce((a, n) => a + flt(n), 0) > flt(s.max) + 1e-6);
		return over ? __("{0}: at most {1} portions", [esc(over.label), over.max]) : null;
	}

	// Only the slots that changed: the others keep their choice (a shared one too).
	sides_choice(r, values) {
		const choice = {};
		r.slots.forEach((s, si) => {
			const items = {};
			let changed = false;
			s.options.forEach((opt, oi) => {
				const n = flt(values[si][oi]);
				if (Math.abs(n - flt(opt.total)) > 0.0005) changed = true; // a shown 0.333 is the stored 1/3
				if (n > 0) items[opt.item] = n;
			});
			if (changed) choice[s.key] = items;
		});
		return choice;
	}

	async sides_dialog(lines, together = false) {
		const o = this.order;
		const r = await call("get_sides", { order: o.name, lines: JSON.stringify(lines), together: together ? 1 : 0 });
		let d = null;
		const values = () =>
			this.sides_values(
				r,
				(f) => d.get_value(f),
				(si) => d.$wrapper.find(`input[name="rfs_${si}"]:checked`).val()
			);
		const fields = this.sides_fields(r, () => show_extras());
		const priced = r.slots.some((s) => s.free && s.options.some((opt) => opt.price));
		if (priced) fields.push({ fieldtype: "HTML", fieldname: "extras_preview" });
		// What the extra portions will add, worked out as the server does: the recipe's own sides
		// are free first, then the dearest options (sides.py split_extras). For several dishes the
		// set's total is shared out, so this is the amount for all of them together.
		const show_extras = () => {
			if (!priced || !d) return;
			const now = values();
			let total = 0;
			r.slots.forEach((s, si) => {
				if (!s.free) return;
				let left = flt(s.free);
				s.options
					.map((opt, oi) => ({ ...opt, n: flt(now[si][oi]) }))
					.sort((a, b) => (b.default || 0) - (a.default || 0) || flt(b.price) - flt(a.price) || (a.item < b.item ? -1 : a.item > b.item ? 1 : 0))
					.forEach((opt) => {
						const take = Math.min(opt.n, Math.max(left, 0));
						left -= take;
						if (opt.price) total += (opt.n - take) * flt(opt.price);
					});
			});
			if (r.per_dish) total *= flt(r.qty) || 1;
			d.fields_dict.extras_preview.$wrapper.html(
				`<p><b>${__("Extra portions to charge: {0}", [money(total)])}</b>${r.per_dish && flt(r.qty) > 1 ? ` <span class="text-muted">(${__("for {0} dishes", [r.qty])})</span>` : ""}</p>`
			);
		};
		d = new frappe.ui.Dialog({
			title: esc(r.title || __("Change Sides")),
			fields,
			primary_action_label: __("Save"),
			primary_action: async () => {
				const now = values();
				const over = this.sides_over(r, now);
				if (over) return frappe.msgprint(over);
				const choice = this.sides_choice(r, now);
				if (!Object.keys(choice).length) return d.hide();
				await call("set_sides", { order: o.name, lines: JSON.stringify(lines), sides_choice: JSON.stringify(choice), together: together ? 1 : 0 }, true);
				d.hide();
				this.load_order(o.name);
			},
		});
		d.show();
		d.$wrapper.find(".rf-radios input").on("change", () => show_extras());
		r.slots.forEach((s, si) => {
			if (s.shared) return;
			s.options.forEach((opt, oi) => {
				const f = d.fields_dict[`s_${si}_${oi}`];
				if (!f || f.df.fieldtype === "Check") return;
				f.$input.attr({ min: 0, max: s.max, inputmode: "numeric" }).on("change", () => {
					const n = flt(f.get_value());
					if (n > flt(s.max)) f.set_value(s.max);
				});
			});
		});
		show_extras();
		// One tap selects the number, so typing replaces it.
		d.$wrapper.find("input").on("focus", function () { this.select && this.select(); });
	}

	discount() {
		const o = this.order;
		frappe.prompt(
			[
				{ fieldname: "percentage", fieldtype: "Percent", label: __("Discount %"), default: o.discount_percentage },
				{ fieldname: "amount", fieldtype: "Currency", label: __("Or Discount Amount"), default: o.discount_percentage ? 0 : o.discount_amount },
				{ fieldname: "reason", fieldtype: "Data", label: __("Discount Reason"), default: o.discount_reason },
			],
			async (v) => {
				const total = flt(o.net_total);
				const percent = flt(v.percentage) || (total ? (flt(v.amount) / total) * 100 : 0);
				// Above the waiter's or cashier's limit: a manager approves it here (D-204).
				const limit = flt(this.floor.discount_limit);
				let ok = {};
				if (percent > limit + 1e-6) {
					ok = await ask_approval(this.floor, __("Discount of {0}% (above {1}%)", [percent.toFixed(2), limit]));
					if (!ok) return;
				}
				await call("apply_discount", { order: o.name, ...v, ...ok }, true);
				this.load_order(o.name);
			},
			__("Discount")
		);
	}

	edit_info(new_customer, focus) {
		const o = this.order;
		// A new customer: nothing of the order's current customer is picked or filled in.
		const c = new_customer ? { ...o, customer: null, customer_name: "", phone: "" } : o;
		const d = new frappe.ui.Dialog({
			title: new_customer ? __("New Customer") : __("Guests and Customer"),
			fields: [
				{ fieldname: "guests", fieldtype: "Int", label: __("Guests"), default: o.guests },
				...this.customer_fields(c, o.order_type === "Delivery"),
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), default: o.notes },
			],
			primary_action_label: __("Save"),
			primary_action: async (v) => {
				// No customer picked clears the link (a name typed alone stays on the order).
				await call("update_order_info", { order: o.name, ...this.customer_values(d, v) }, true);
				d.hide();
				this.load_order(o.name);
			},
		});
		d.show();
		this.bind_customer(d, c, !!new_customer);
		const first = focus === "notes" ? "notes" : new_customer ? "phone" : null;
		if (first) setTimeout(() => d.fields_dict[first].$input && d.fields_dict[first].$input.trigger("focus"), 300);
	}

	free_tables() {
		return this.floor.halls.flatMap((h) => h.tables.filter((t) => t.status === "free" || t.status === "reserved").map((t) => t.name));
	}

	move() {
		const o = this.order;
		frappe.prompt(
			[{ fieldname: "table", fieldtype: "Select", label: __("Table", null, "Resto"), options: this.free_tables(), reqd: 1 }],
			async (v) => { await call("move_table", { order: o.name, table: v.table }, true); this.load_order(o.name); },
			__("Move to Table")
		);
	}

	merge() {
		const o = this.order;
		const others = this.floor.halls.flatMap((h) => h.tables.filter((t) => t.order && t.order.name !== o.name).map((t) => ({ value: t.order.name, label: `${t.name} · ${t.order.name}` })));
		if (!others.length) return frappe.show_alert({ message: __("No other open tables"), indicator: "blue" });
		frappe.prompt(
			[{ fieldname: "source", fieldtype: "Select", label: __("Table to merge into this one"), options: others, reqd: 1 }],
			async (v) => { await call("merge_orders", { target: o.name, source: v.source }, true); this.load_order(o.name); },
			__("Merge Tables")
		);
	}

	delivery() {
		const o = this.order;
		frappe.prompt(
			[
				{ fieldname: "delivery_status", fieldtype: "Select", label: __("Delivery Status"), options: ["Pending", "Out for Delivery", "Delivered"].map((s) => ({ value: s, label: __(s, null, "Resto") })), default: o.delivery_status || "Pending" },
				{ fieldname: "driver", fieldtype: "Link", options: "Employee", label: __("Driver", null, "Resto"), default: o.driver },
				{ fieldname: "delivery_fee", fieldtype: "Currency", label: __("Delivery Fee"), default: o.delivery_fee },
			],
			async (v) => { await call("set_delivery", { order: o.name, ...v }, true); this.load_order(o.name); },
			__("Delivery", null, "Resto")
		);
	}

	service() {
		const o = this.order;
		frappe.prompt(
			[{ fieldname: "percent", fieldtype: "Percent", label: __("Service Charge %"), default: o.service_charge_percent }],
			async (v) => { await call("set_service_charge", { order: o.name, percent: v.percent }, true); this.load_order(o.name); },
			__("Service Charge")
		);
	}

	cancel() {
		const o = this.order;
		const sent = (o.items || []).filter((d) => !["Not Sent", "Void"].includes(d.kitchen_status));
		const fields = [{ fieldname: "reason", fieldtype: "Small Text", label: __("Cancel Reason"), reqd: 1 }];
		if (sent.length) {
			// What the kitchen already made is posted as waste; what it did not make stays in stock.
			fields.push({
				fieldname: "cooked_help",
				fieldtype: "HTML",
				options: `<p><b>${__("Already cooked?")}</b> <span class="text-muted">${__("Tick the dishes the kitchen already made: their ingredients are posted as waste.")}</span></p>`,
			});
			fields.push(
				...resto.cooked_fields(
					sent,
					(d) => `${esc(d.item_name)} × ${d.qty} (${esc(__(d.kitchen_status, null, "Resto"))})`,
					(d) => ["In Progress", "Ready", "Served"].includes(d.kitchen_status)
				)
			);
		}
		frappe.prompt(
			fields,
			async (v) => {
				const cooked = resto.cooked_values(sent, v, (d) => d.name);
				const ok = sent.length ? await ask_approval(this.floor, __("Cancel order {0} with items sent to the kitchen", [o.name])) : {};
				if (!ok) return;
				await call("cancel_order", { order: o.name, reason: v.reason, cooked: JSON.stringify(cooked), ...ok }, true);
				this.close_order();
			},
			__("Cancel Order"),
			__("Cancel Order")
		);
	}

	// ------------------------------------------------------------------ payment

	// Payment (D-214): the bill on one side; on the other the methods, quick amounts and a keypad.
	// Each method holds one amount; several methods make a split payment. The server checks it all
	// again (settle), with the amount the cashier saw.
	async pay(lines) {
		const o = this.order;
		const bill = await call("get_bill", { order: o.name, lines: lines ? JSON.stringify(lines) : null }, true);
		// A packaging or consumable short in stock: said before the money is taken (D-224).
		if (bill.stock_warning) frappe.msgprint({ message: bill.stock_warning, indicator: "orange", title: __("Stock") });
		const modes = bill.payment_modes;
		const payable = flt(bill.payable);
		const amounts = modes.map(() => 0);
		let current = Math.max(modes.findIndex((m) => m.default), 0);
		let entry = "";
		let on_account = false;
		// The answer to Complete did not arrive: the amounts stay as sent until it is pressed again.
		let unsure = false;
		// One id per dialog: a retried click after a lost answer returns the same invoice (the server
		// checks it is the same amounts on the same methods).
		const request_id = frappe.utils.get_random(20);
		const P = cint((frappe.boot.sysdefaults || {}).currency_precision) || 2;
		const EPS = 0.5 / 10 ** P;
		const round = (v) => flt(v, P);
		// Typed and quick amounts are plain "12.5" strings: read with a point whatever the number format.
		const num = (v) => round(typeof v === "number" ? v : parseFloat(String(v)) || 0);
		const is_cash = (i) => i >= 0 && modes[i].type === "Cash";
		// The cash method for "Exact Amount in Cash": the default one when it is cash; none: no fast path.
		const cash = is_cash(modes.findIndex((m) => m.default)) ? modes.findIndex((m) => m.default) : modes.findIndex((m) => m.type === "Cash");
		const paid = () => amounts.reduce((s, a) => s + flt(a), 0);
		const left = () => round(payable - paid());
		const non_cash = () => amounts.reduce((s, a, i) => (is_cash(i) ? s : s + flt(a)), 0);
		// What the highlighted method needs to complete the bill, with the other methods' amounts.
		const need = () => Math.max(round(payable - (paid() - flt(amounts[current]))), 0);
		const busy = () => !!this._settling || unsure;
		let quicks = [];
		const quick = () => {
			const n = need();
			if (!n) return [];
			// Rounded-up notes only for cash: a card is charged the exact amount.
			const ups = is_cash(current) ? [50, 100, 200, 500, 1000].map((step) => Math.ceil(n / step) * step).filter((v) => v > n + EPS) : [];
			return [n, ...[...new Set(ups)].slice(0, 3)];
		};

		const d = new frappe.ui.Dialog({ title: __("Payment"), size: "extra-large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		const $w = d.fields_dict.html.$wrapper;
		const where = o.order_type === "Dine In" ? __("Table {0}", [esc(o.resto_table)], "Resto") : __(o.order_type, null, "Resto");
		d.set_title(`${lines ? __("Pay Selected Items") : __("Payment")} · ${esc(o.name)}`);
		const items = bill.items
			.map((i) => `<div class="rf-prow"><span>${esc(i.item_name)} × ${fmt_qty(i.qty)}</span><span class="rf-spacer"></span><span>${money(i.amount)}</span></div>`)
			.join("");
		const taxes = bill.taxes.map((t) => `<div class="rf-trow"><span>${esc(t.description)}</span><span class="rf-spacer"></span><span>${money(t.amount)}</span></div>`).join("");
		$w.html(`
			<div class="rf-root rf-pay">
				<section class="rf-pbill">
					<div class="rf-muted">${where} · ${esc(o.name)}</div>
					<div class="rf-pcust"><b>${esc(bill.on_account_customer_name || o.customer_name || __("Walk-in", null, "Resto"))}</b><span class="rf-spacer"></span>
						<button class="rf-btn rf-sm" data-p="customer">${__("Change Customer")}</button></div>
					<div class="rf-plines">${items}
						<span class="rf-spacer"></span>
						<div class="rf-totals rf-ptot">
							<div class="rf-trow"><span>${__("Items Total")}</span><span class="rf-spacer"></span><span>${money(bill.total)}</span></div>
							${bill.discount ? `<div class="rf-trow"><span>${__("Discount")}</span><span class="rf-spacer"></span><span dir="ltr">−${money(bill.discount)}</span></div>` : ""}
							${taxes}
							${bill.rounding_adjustment ? `<div class="rf-trow"><span>${__("Rounding")}</span><span class="rf-spacer"></span><span>${money(bill.rounding_adjustment)}</span></div>` : ""}
							<div class="rf-grand"><b>${__("To Pay")}</b><span class="rf-spacer"></span><b class="rf-gv">${money(payable)}</b></div>
							${bill.guests > 1 ? `<div class="rf-trow rf-muted"><span>${__("Per Guest")} (${bill.guests})</span><span class="rf-spacer"></span><span>${money(bill.per_guest)}</span></div>` : ""}
						</div>
					</div>
					<div class="rf-ptiles"><div class="rf-ptile b"><span>${__("Paid", null, "Resto")}</span><b class="rf-paid"></b></div>
						<div class="rf-ptile rf-rest"><span></span><b></b></div></div>
				</section>
				<section class="rf-pkeys">
					<div class="rf-pmodes">${modes.map((m, i) => `<button class="rf-mth" data-m="${i}"><span>${esc(__(m.mode_of_payment))}</span><small></small></button>`).join("")}</div>
					<div><span class="rf-muted rf-plabel">${__("Quick Amounts")}</span><div class="rf-quicks"></div></div>
					<div class="rf-pinput">
						<div class="rf-pentry"><span class="rf-pmode"></span><span class="rf-spacer"></span><b dir="ltr" class="rf-pval"></b></div>
						<div class="rf-pad">
							<div class="rf-keys">${["7", "8", "9", "4", "5", "6", "1", "2", "3", "00", "0", "."].map((k) => `<button class="rf-k" data-k="${k}">${k}</button>`).join("")}</div>
							<div class="rf-kside">
								<button class="rf-k rf-kdel" data-k="back" aria-label="${__("Delete")}">⌫</button>
								<button class="rf-k rf-kclr" data-k="clear">${__("Clear")}</button>
								<button class="rf-k rf-kadd" data-k="add">${__("Add", null, "Resto")}</button>
								<div class="rf-added"></div>
							</div>
						</div>
					</div>
					${bill.on_account_customer ? `<label class="rf-onacc" style="display:none"><input type="checkbox"> <span>${__("Leave the rest on the account of {0}", [esc(bill.on_account_customer_name || bill.on_account_customer)])}</span></label>` : ""}
					${o.order_type === "Dine In" && o.items.some((x) => !["Ready", "Served", "Void"].includes(x.kitchen_status)) ? `<div class="rf-warn rf-pwarn">⚠ ${__("Some dishes are not out of the kitchen yet: the table stays taken until they are ready.")}</div>` : ""}
					<div class="rf-pnote rf-muted"></div>
					<div class="rf-pacts">
						<button class="rf-btn rf-lg" data-p="exact"><span>${__("Exact Amount in Cash")}</span><kbd dir="ltr">Enter</kbd></button>
						<button class="rf-btn rf-btn-p rf-lg rf-pdone" data-p="done"></button>
					</div>
					${lines ? "" : `<button class="rf-btn rf-sm rf-psplit" data-p="split">${__("Split by Items")}</button>`}
				</section>
			</div>`);

		const draw = () => {
			const rest = left();
			$w.find(".rf-mth").each((i, el) => {
				$(el).toggleClass("on", i === current).find("small").text(flt(amounts[i]) ? money(amounts[i]) : "");
			});
			quicks = quick();
			$w.find(".rf-quicks").html(
				quicks
					.map((v, i) => `<button class="rf-qa ${i === 0 ? "exact" : ""}" data-q="${i}">${money(v)}${i === 0 ? ` ${__("exact", null, "Resto")}` : ""}</button>`)
					.join("")
			);
			$w.find(".rf-pmode").text(__(modes[current].mode_of_payment));
			$w.find(".rf-pval").text(entry || "0");
			$w.find(".rf-added").html(
				amounts.some((a) => flt(a))
					? `<span class="rf-muted">${__("Added", null, "Resto")}</span>` +
							modes.map((m, i) => (flt(amounts[i]) ? `<div class="rf-trow"><b>${esc(__(m.mode_of_payment))}</b><span class="rf-spacer"></span><span>${money(amounts[i])}</span></div>` : "")).join("")
					: ""
			);
			$w.find(".rf-paid").text(money(paid()));
			const $rest = $w.find(".rf-rest");
			if (rest > EPS) {
				$rest.removeClass("g").addClass("r").find("span").text(__("Remaining"));
				$rest.find("b").text(money(rest));
			} else {
				$rest.removeClass("r").addClass("g").find("span").text(__("Change", null, "Resto"));
				$rest.find("b").text(money(-rest));
			}
			const $acc = $w.find(".rf-onacc");
			$acc.toggle(rest > EPS);
			const over = non_cash() > payable + EPS;
			let note = "";
			if (unsure) note = __("The answer did not arrive. Press Complete Payment again to check: the bill is not taken twice.");
			else if (over) note = __("Card and bank payments cannot be more than the bill; only cash can give change");
			else if (rest > EPS && !bill.on_account_customer && bill.allow_partial_payment) {
				note = __("To leave the rest owed, choose the customer in Guests and Customer first (not the walk-in customer).");
			}
			$w.find(".rf-pnote").text(note).toggleClass("rf-warn", unsure || over);
			const done = $w.find(".rf-pdone");
			// Saving: the button says so at once (a payment takes a moment on a small server).
			done.toggleClass("rf-busy", !!this._settling);
			done.html(
				this._settling
					? `${ICON.spin}<span>${__("Saving…")}</span>`
					: rest > EPS
					? on_account ? __("Complete Payment · {0} on account", [money(rest)]) : __("Complete Payment · {0} left", [money(rest)])
					: __("Complete Payment · Change {0}", [money(-rest)])
			);
			done.prop("disabled", !!this._settling || over || (rest > EPS && !(on_account && $acc.is(":visible"))));
			$w.find("[data-p=exact]").toggle(cash >= 0).prop("disabled", busy());
			$w.find(".rf-k, .rf-qa, .rf-mth, .rf-onacc input, [data-p=customer], [data-p=split]").prop("disabled", busy());
		};
		const set_amount = (v) => {
			let a = Math.max(num(v), 0);
			if (!is_cash(current) && a > need() + EPS) {
				a = need();
				frappe.show_alert({ message: __("Card and bank payments cannot be more than the bill; only cash can give change"), indicator: "orange" });
			}
			amounts[current] = a;
			entry = "";
			draw();
		};
		const key = (k) => {
			if (busy()) return;
			if (k === "back") entry = entry.slice(0, -1);
			else if (k === "clear") {
				entry = "";
				amounts[current] = 0;
			} else if (k === "add") return set_amount(entry || need());
			else if (k === "." && entry.includes(".")) return;
			else if ((entry + k).length <= 10) entry = (entry === "0" && k !== "." ? "" : entry) + k;
			draw();
		};
		const settle = async () => {
			if (this._settling) return;
			const rest = left();
			const owed = rest > EPS && on_account ? rest : 0;
			if ((rest > EPS && !owed) || non_cash() > payable + EPS) return;
			// What is sent is what was on the screen when Complete was pressed.
			const payments = modes.map((m, i) => ({ mode_of_payment: m.mode_of_payment, amount: round(amounts[i]) }));
			this._settling = true;
			draw();
			let r;
			try {
				let ok = {};
				if (owed && bill.on_account_needs_approval) {
					// The customer has no credit limit: a manager approves this amount on account.
					ok = await ask_approval(this.floor, __("{0} on the account of {1} (no credit limit)", [money(owed), bill.on_account_customer_name || bill.on_account_customer]));
					if (!ok) return;
				}
				r = await call(
					"settle",
					{
						order: o.name,
						payments: JSON.stringify(payments),
						lines: lines ? JSON.stringify(lines) : null,
						expected: bill.payable,
						request_id,
						on_account: owed ? 1 : 0,
						...ok,
					},
					false, // the button shows it is saving; the window is not greyed out
					60000
				);
			} catch (e) {
				// Not reached: it may have been billed. The amounts stay as sent; pressing again asks
				// the server with the same id, which returns that invoice or bills it once.
				if (e && e.network) unsure = true;
				else if (!(e && "network" in e)) console.error(e);
				// refused: the server's message is shown, the window stays as it was
			} finally {
				this._settling = false;
				draw();
			}
			if (!r) return;
			unsure = false;
			d.hide();
			this.paid_dialog(r, bill);
			if (r.status === "Paid") this.close_order();
			else this.load_order(o.name);
		};
		const exact_cash = () => {
			if (cash < 0 || busy()) return;
			const others = amounts.reduce((s, a, i) => (i === cash ? s : s + flt(a)), 0);
			current = cash;
			amounts[cash] = Math.max(round(payable - others), 0);
			entry = "";
			draw();
			settle();
		};

		// A method tapped after typing takes the typed amount (Enter or Add puts it there).
		$w.on("click", ".rf-mth", (e) => {
			if (busy()) return;
			current = cint($(e.currentTarget).attr("data-m"));
			draw();
		});
		$w.on("click", ".rf-qa", (e) => {
			if (busy()) return;
			const v = quicks[cint($(e.currentTarget).attr("data-q"))];
			if (v != null) set_amount(v);
		});
		$w.on("click", ".rf-k", (e) => key($(e.currentTarget).attr("data-k")));
		$w.on("change", ".rf-onacc input", (e) => {
			if (busy()) return;
			on_account = $(e.currentTarget).is(":checked");
			draw();
		});
		$w.on("click", "[data-p]", (e) => {
			const p = $(e.currentTarget).attr("data-p");
			if (p === "done") return settle();
			if (p === "exact") return exact_cash();
			if (busy()) return;
			d.hide();
			if (p === "split") return this.split();
			if (p === "customer") return this.edit_info();
		});
		// The keyboard: digits (Arabic ones too) and the point type, Backspace deletes. Enter adds what
		// is typed to the highlighted method; with nothing typed it pays when the bill is covered (or
		// the rest goes on account), pays the exact amount in cash when cash is highlighted and nothing
		// is added yet, and otherwise puts what is left on the highlighted method (Enter again pays).
		d.$wrapper.on("keydown", (e) => {
			if ($(e.target).is("input[type=checkbox], [data-p=customer], [data-p=split]")) return;
			const k = String(e.key || "")
				.replace(/[\u0660-\u0669]/g, (c) => String(c.charCodeAt(0) - 0x660))
				.replace(/[\u06f0-\u06f9]/g, (c) => String(c.charCodeAt(0) - 0x6f0));
			if (/^[0-9.]$/.test(k)) key(k);
			else if (k === "Backspace") key("back");
			else if (k === "Enter") {
				e.preventDefault();
				if (this._settling) return;
				if (unsure) settle();
				else if (entry) key("add");
				else if (left() <= EPS) settle();
				else if (!paid() && is_cash(current) && current === cash) exact_cash();
				else if (on_account && $w.find(".rf-onacc").is(":visible")) settle();
				else key("add");
			} else return;
			e.preventDefault();
		});
		draw();
		this._paying = true;
		d.$wrapper.on("hidden.bs.modal", () => (this._paying = false));
		d.show();
		setTimeout(() => d.$wrapper.find(".modal-content").attr("tabindex", -1).trigger("focus"), 200);
	}

	// Paid: the real invoice number, the change, what is left owed, and the receipt.
	paid_dialog(r, bill) {
		this.receipt_mode = r.receipt_mode;
		const print_receipt = () => this.print_receipt(r.sales_invoice, r.receipt_mode);
		// Printed once by itself, on the device that prints receipts: a retried payment (same
		// invoice) does not print it again.
		const auto = r.receipt_mode === "auto" && !r.receipt_printed && this.prints_here();
		frappe.msgprint({
			title: __("Paid"),
			indicator: "green",
			message: `<div class="rf-paidmsg">${__("Invoice")} <a href="/app/sales-invoice/${esc(r.sales_invoice)}"><b>${esc(r.sales_invoice)}</b></a>
				${r.change_amount ? `<div class="rf-paidchg">${__("Change", null, "Resto")}: <b>${money(r.change_amount)}</b></div>` : ""}
				${r.outstanding_amount > 0.005 ? `<div class="text-warning"><b>${__("On account of {0}: {1}", [esc(bill.on_account_customer_name || bill.on_account_customer), money(r.outstanding_amount)])}</b></div>` : `<div class="rf-paidfull">${__("Paid in Full")} ✓</div>`}</div>`,
			primary_action: {
				// The receipt printed by itself (cashier printer, or this browser): this prints it again.
				label: r.receipt_mode === "server" || auto || r.receipt_printed ? __("Print Again") : __("Print Receipt"),
				action: () => print_receipt(),
			},
		});
		if (auto) print_receipt();
	}

	split() {
		const o = this.order;
		const open = o.items.filter((d) => d.kitchen_status !== "Void" && d.qty - (d.billed_qty || 0) > 0);
		const fields = open.map((d, i) => ({
			fieldname: `l_${i}`,
			fieldtype: "Float",
			label: `${esc(d.item_name)}${d.modifiers_text ? " (" + esc(d.modifiers_text) + ")" : ""} · ${money(flt(d.qty) ? flt(d.amount) / flt(d.qty) : d.rate + (d.modifiers_amount || 0) + (d.sides_amount || 0))} · ${__("left")} ${d.qty - (d.billed_qty || 0)}`,
			default: 0,
		}));
		const d = new frappe.ui.Dialog({
			title: __("Split by Items"),
			fields,
			primary_action_label: __("Pay These"),
			primary_action: (v) => {
				const lines = {};
				open.forEach((row, i) => { if (v[`l_${i}`] > 0) lines[row.name] = v[`l_${i}`]; });
				if (!Object.keys(lines).length) return frappe.msgprint(__("Enter a quantity for at least one item"));
				d.hide();
				this.pay(lines);
			},
		});
		d.show();
	}

	static css() {
		return `
		.rf-cust { display:block; width:100%; text-align:start; margin-bottom:4px; white-space:normal; }
		.rf-cust-picked { font-weight:600; color:var(--green-700, #2f855a); margin-bottom:6px; }
		.rf-metrics { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:8px; margin-bottom:10px; }
		.rf-metric { border:1px solid var(--border-color); border-radius:8px; padding:6px 10px; }
		.rf-xparts summary { cursor:pointer; }
		.rf-xline { display:flex; justify-content:space-between; gap:12px; font-size:12px; color:var(--text-muted); min-width:220px; }
		.rf-xline.rf-xtotal { border-top:1px solid var(--border-color); color:var(--text-color); font-weight:600; margin-top:2px; }
		.rf-print-alert { flex-basis:100%; border-radius:8px; padding:6px 10px; margin-bottom:6px; font-weight:600; background:var(--red-100, #fed7d7); color:var(--red-800, #9b2c2c); border:2px solid var(--red-500, #f56565); }
		.rf-offline { position:sticky; top:var(--navbar-height, 48px); z-index:5; height:40px; line-height:40px; padding:0 12px; overflow:hidden;
			white-space:nowrap; text-overflow:ellipsis; background:var(--rf-amber-soft, #FFF5E0); color:var(--rf-amber-ink, #78350F); font-weight:700; text-align:center; }
		.rf-root.rf-off .rf-order { height:calc(100vh - var(--navbar-height, 48px) - 40px); height:calc(100dvh - var(--navbar-height, 48px) - 40px); }
		.rf-amt { text-align:end; white-space:nowrap; }
		.rf-quick { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
		.rf-bill td { padding:4px 8px !important; }
		/* ---- the floor's look (D-214): its own palette, Frappe's colours in dark mode ---- */
		.rf-page .page-head { display:none; }
		.rf-page .container.page-body, .rf-page .page-body { max-width:100% !important; width:100%; padding:0 !important; }
		.rf-page .layout-main-section-wrapper, .rf-page .layout-main, .rf-page .page-content { padding:0 !important; margin:0 !important; }
		.rf-page .layout-main-section { border:0 !important; background:transparent !important; padding:0 !important; box-shadow:none !important; }
		.rf-root {
			--rf-bg:#F6F5F2; --rf-card:#fff; --rf-ink:#1C1B19; --rf-ink2:#3A3732; --rf-muted:#5E5A53; --rf-line:#E4E1DA; --rf-line2:#F1EFEA; --rf-soft:#F1EFEA;
			--rf-acc:#C2410C; --rf-acc-soft:#FFF1E8; --rf-acc-ink:#9A3412; --rf-acc-tint:#FFFBF7;
			--rf-green:#15803D; --rf-green-soft:#E9F7EE; --rf-green-ink:#14532D; --rf-green-line:#BFE3CB;
			--rf-blue:#1D4ED8; --rf-blue-soft:#EAF0FF; --rf-blue-ink:#1E3A8A;
			--rf-amber:#B45309; --rf-amber-soft:#FFF5E0; --rf-amber-ink:#78350F;
			--rf-red:#B91C1C; --rf-red-soft:#FDECEC; --rf-red-ink:#7F1D1D;
			--rf-purple:#7E22CE; --rf-purple-soft:#F5EDFF; --rf-purple-ink:#581C87;
			background:var(--rf-bg); color:var(--rf-ink); min-height:calc(100vh - var(--navbar-height, 48px));
			font-family:var(--font-stack, inherit);
		}
		[data-theme="dark"] .rf-root {
			--rf-bg:var(--bg-color); --rf-card:var(--card-bg); --rf-ink:var(--text-color); --rf-ink2:var(--text-color); --rf-muted:var(--text-muted);
			--rf-line:var(--border-color); --rf-line2:var(--border-color); --rf-soft:var(--control-bg);
			--rf-acc-soft:#3b2116; --rf-acc-ink:#fdba8c; --rf-acc-tint:#2a1a12;
			--rf-green-soft:#12301f; --rf-green-ink:#86efac; --rf-green-line:#1f5134;
			--rf-blue-soft:#172554; --rf-blue-ink:#93c5fd; --rf-amber-soft:#3a2a0c; --rf-amber-ink:#fcd34d;
			--rf-red-soft:#3b1414; --rf-red-ink:#fca5a5; --rf-purple-soft:#2e1446; --rf-purple-ink:#d8b4fe;
			--rf-red:#ef4444; --rf-green:#22c55e; --rf-blue:#3b82f6; --rf-amber:#f59e0b; --rf-purple:#a855f7;
		}
		.rf-root svg { width:18px; height:18px; flex-shrink:0; }
		.rf-root button { font-family:inherit; }
		.rf-spacer { flex:1; }
		.rf-muted { color:var(--rf-muted); }
		.rf-empty { color:var(--rf-muted); padding:16px; }
		.rf-ib { width:44px; height:44px; border-radius:10px; border:1px solid var(--rf-line); background:var(--rf-card); color:var(--rf-ink2);
			display:inline-flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; padding:0; }
		.rf-ib.rf-flat { border:0; width:44px; height:44px; }
		.rf-ib.g { color:var(--rf-green-ink); }
		.rf-btn { min-height:44px; padding:0 16px; border-radius:10px; border:1px solid var(--rf-line); background:var(--rf-card); color:var(--rf-ink);
			font-weight:700; font-size:15px; display:inline-flex; align-items:center; justify-content:center; gap:8px; cursor:pointer; white-space:nowrap; }
		.rf-btn:disabled { opacity:.5; cursor:not-allowed; }
		.rf-btn.rf-done:disabled { opacity:1; cursor:default; background:var(--rf-green-soft); border-color:var(--rf-green-soft); color:var(--rf-green-ink); }
		.rf-btn-p { background:var(--rf-acc); border-color:var(--rf-acc); color:#fff; }
		.rf-btn-d { background:var(--rf-ink); border-color:var(--rf-ink); color:var(--rf-card); }
		.rf-btn.rf-sm, .rf-ib.rf-sm { min-height:44px; height:44px; font-size:13px; padding:0 12px; }
		.rf-ib.rf-sm { width:44px; padding:0; }
		.rf-btn.rf-lg { min-height:52px; }
		.rf-btn.rf-xl { min-height:56px; font-size:17px; width:100%; }
		.rf-pill { height:36px; padding:0 12px; border-radius:999px; border:0; display:inline-flex; align-items:center; gap:8px; font-size:14px; font-weight:600; white-space:nowrap; }
		button.rf-pill { cursor:pointer; height:44px; }
		.rf-pill-green { background:var(--rf-green-soft); color:var(--rf-green-ink); }
		.rf-pill-amber { background:var(--rf-amber-soft); color:var(--rf-amber-ink); }
		.rf-pill-gray { background:var(--rf-soft); color:var(--rf-ink2); }
		.rf-net.ok svg { color:var(--rf-green); }
		.rf-dotc { width:8px; height:8px; border-radius:50%; background:currentColor; display:inline-block; }
		.rf-pill-green .rf-dotc { background:var(--rf-green); }
		.rf-cnt { min-width:22px; height:22px; padding:0 6px; border-radius:999px; background:var(--rf-soft); color:var(--rf-ink); font-size:12px; font-weight:700;
			display:inline-flex; align-items:center; justify-content:center; }
		.rf-head { min-height:64px; background:var(--rf-card); border-bottom:1px solid var(--rf-line); display:flex; align-items:center; gap:12px; padding:8px 20px; flex-wrap:wrap; }
		.rf-brand { display:flex; align-items:center; gap:10px; }
		.rf-brand > div { display:flex; flex-direction:column; line-height:1.25; }
		.rf-brand small, .rf-otitle small, .rf-custtxt small { font-size:13px; color:var(--rf-muted); }
		.rf-logo { width:40px; height:40px; border-radius:10px; background:var(--rf-acc); color:#fff; display:flex; align-items:center; justify-content:center; }
		.rf-logo svg { width:22px; height:22px; }
		.rf-userbtn { gap:10px; padding:0 10px; }
		.rf-av { width:30px; height:30px; border-radius:50%; background:var(--rf-acc-soft); color:var(--rf-acc-ink); display:inline-flex; align-items:center; justify-content:center; font-size:14px; }
		.rf-av.b { width:34px; height:34px; background:var(--rf-blue-soft); color:var(--rf-blue-ink); }
		.rf-alerts:empty { display:none; }
		.rf-alerts { padding:12px 20px 0; display:flex; flex-direction:column; gap:8px; }
		.rf-banner { display:flex; align-items:center; gap:14px; padding:12px 16px; border-radius:12px; background:var(--rf-amber-soft); color:var(--rf-amber-ink); border:1px solid var(--rf-amber); }
		.rf-banner > div { flex:1; display:flex; flex-direction:column; gap:2px; }
		.rf-banner.soft { border-color:var(--rf-line); background:var(--rf-card); color:var(--rf-ink2); justify-content:space-between; }
		.rf-bar { min-height:72px; display:flex; align-items:center; gap:8px; padding:8px 20px; flex-wrap:wrap; }
		.rf-tabs { display:flex; gap:4px; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; padding:4px; flex-wrap:wrap; }
		.rf-tab { min-height:44px; padding:0 16px; border-radius:10px; border:0; background:transparent; color:var(--rf-muted); font-weight:700; font-size:15px;
			display:inline-flex; align-items:center; gap:8px; cursor:pointer; }
		.rf-tab.on { background:var(--rf-ink); color:var(--rf-card); }
		.rf-tab.on .rf-cnt { background:var(--rf-ink2); color:var(--rf-card); }
		.rf-acts { display:flex; gap:8px; flex-wrap:wrap; }
		.rf-main { display:flex; gap:20px; padding:0 20px 20px; align-items:flex-start; }
		.rf-body { flex:1; min-width:0; display:flex; flex-direction:column; gap:16px; }
		.rf-side { width:340px; flex-shrink:0; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:14px; overflow:hidden; }
		.rf-side-h { height:56px; display:flex; align-items:center; gap:8px; padding:0 16px; border-bottom:1px solid var(--rf-line); font-size:16px; }
		.rf-side-l { display:flex; flex-direction:column; max-height:calc(100vh - 260px); overflow:auto; }
		.rf-grid { display:grid; gap:16px; }
		.rf-grid.locked .rf-tbl { opacity:.5; filter:grayscale(.6); }
		.rf-tbl { position:relative; border-radius:14px; border:2px solid var(--rf-line); padding:16px; display:flex; flex-direction:column; gap:10px; min-height:168px;
			text-align:start; cursor:pointer; background:var(--rf-card); color:var(--rf-ink); user-select:none; }
		.rf-tbl:active { transform:scale(.98); }
		.rf-tbl.round { border-radius:28px; }
		.rf-ttop, .rf-tfoot { display:flex; align-items:center; gap:8px; width:100%; }
		.rf-ttop { flex-wrap:wrap; row-gap:4px; }
		.rf-tbl { min-width:0; }
		.rf-main { container-type:inline-size; }
		@container (max-width:1180px) { .rf-side { width:260px; } }
		@container (max-width:900px) { .rf-side { display:none; } }
		.rf-tfoot { margin-top:auto; align-items:baseline; flex-wrap:wrap; row-gap:2px; }
		.rf-tname { font-size:28px; line-height:32px; }
		.rf-tsub { font-size:14px; color:var(--rf-ink2); }
		.rf-tamt { font-size:22px; white-space:nowrap; }
		.rf-tago { font-size:14px; font-weight:700; white-space:nowrap; }
		@media (max-width:1180px) { .rf-side { width:280px; } .rf-main { gap:14px; padding:0 14px 14px; } .rf-tbl { padding:12px; } .rf-tamt { font-size:19px; } }
		.rf-thint { font-size:14px; color:var(--rf-muted); }
		.rf-st { height:26px; padding:0 10px; border-radius:999px; font-size:13px; font-weight:700; display:inline-flex; align-items:center; white-space:nowrap; }
		.rf-tbl.free { border-color:var(--rf-green-line); }
		.rf-tbl.free .rf-st { background:var(--rf-green-soft); color:var(--rf-green-ink); }
		.rf-tbl.occupied { background:var(--rf-blue-soft); border-color:var(--rf-blue); }
		.rf-tbl.occupied .rf-st { background:var(--rf-blue); color:#fff; } .rf-tbl.occupied .rf-tago { color:var(--rf-blue-ink); }
		.rf-tbl.bill { background:var(--rf-amber-soft); border-color:var(--rf-amber); }
		.rf-tbl.bill .rf-st { background:var(--rf-amber); color:#fff; } .rf-tbl.bill .rf-tago { color:var(--rf-amber-ink); }
		.rf-tbl.reserved { background:var(--rf-purple-soft); border-color:var(--rf-purple); }
		.rf-tbl.reserved .rf-st { background:var(--rf-purple); color:#fff; } .rf-tres { color:var(--rf-purple-ink); font-weight:700; font-size:14px; }
		.rf-legend { display:flex; gap:20px; align-items:center; font-size:14px; color:var(--rf-ink2); flex-wrap:wrap; }
		.rf-legend span { display:inline-flex; align-items:center; gap:6px; }
		.rf-legend i { width:12px; height:12px; border-radius:4px; display:inline-block; }
		.rf-legend i.free { background:var(--rf-green-soft); border:2px solid var(--rf-green); } .rf-legend i.occupied { background:var(--rf-blue); }
		.rf-tbl.paid { background:var(--rf-card); border:2px dashed var(--rf-green); } .rf-tbl.paid .rf-st { background:var(--rf-green); color:#fff; }
		.rf-ono { color:var(--rf-acc); } .rf-paidfull { color:var(--rf-green); font-weight:700; margin-top:6px; } .rf-legend i.paid { border:2px dashed var(--rf-green); } .rf-pwarn { padding:6px 0; font-weight:700; }
		.rf-legend i.bill { background:var(--rf-amber); } .rf-legend i.reserved { background:var(--rf-purple); }
		.rf-orow { all:unset; box-sizing:border-box; cursor:pointer; display:flex; flex-direction:column; gap:6px; padding:14px 16px; border-bottom:1px solid var(--rf-line2); }
		.rf-orow1, .rf-orow2 { display:flex; align-items:center; gap:8px; }
		.rf-orow1 { font-size:15px; } .rf-orow2 { font-size:13px; color:var(--rf-muted); flex-wrap:wrap; }
		.rf-st.k-takeaway { background:var(--rf-acc-soft); color:var(--rf-acc-ink); }
		.rf-st.k-delivery { background:var(--rf-blue-soft); color:var(--rf-blue-ink); }
		.rf-st.k-rebill { background:var(--rf-red-soft); color:var(--rf-red-ink); }
		.rf-st.k-staff { background:var(--rf-purple-soft); color:var(--rf-purple-ink); }
		.rf-ok { color:var(--rf-green-ink); font-weight:700; } .rf-warn { color:var(--rf-amber-ink); font-weight:700; font-size:13px; }
		.rf-olist { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px,1fr)); gap:12px; }
		.rf-olist .rf-orow { background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; }
		.rf-root.rf-mdlg { min-height:0; background:transparent; }
		.rf-root.rf-lock { position:fixed; top:0; right:0; bottom:0; left:0; z-index:2147483000; min-height:0; display:flex; align-items:center; justify-content:center; padding:16px;
			background:var(--rf-bg); overflow:auto; }
		.rf-lockbox { width:min(380px, 100%); display:flex; flex-direction:column; align-items:center; gap:12px; text-align:center; outline:none; }
		.rf-lockico { width:64px; height:64px; border-radius:20px; background:var(--rf-acc-soft); color:var(--rf-acc); display:flex; align-items:center; justify-content:center; }
		.rf-lockico svg { width:30px; height:30px; }
		.rf-lock-brand { font-size:15px; color:var(--rf-muted); }
		.rf-lock-user { display:flex; align-items:center; gap:10px; font-size:20px; }
		.rf-lock-user .rf-av { width:44px; height:44px; font-size:18px; }
		.rf-dots { display:flex; gap:12px; justify-content:center; min-height:20px; }
		.rf-dots i { width:16px; height:16px; border-radius:50%; border:2px solid var(--rf-ink2); display:inline-block; }
		.rf-dots i.on { background:var(--rf-ink); border-color:var(--rf-ink); }
		.rf-lock-msg { min-height:20px; margin:0; color:var(--rf-red-ink); font-weight:700; }
		.rf-lockkeys { width:100%; display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; }
		.rf-lockkeys .rf-k { height:60px; font-size:24px; }
		.rf-lock-open, .rf-lock-sw { width:100%; }
		.rf-menu-list { display:flex; flex-direction:column; gap:8px; }
		.rf-menu-btn { width:100%; justify-content:flex-start; min-height:48px; }
		.rf-menu-btn.rf-danger { color:var(--rf-red-ink); border-color:var(--rf-red-soft); margin-top:8px; }
		.rf-menu-cols { display:grid; grid-template-columns:1fr 1fr; gap:8px 16px; align-items:start; }
		.rf-menu-foot { margin-top:12px; padding-top:12px; border-top:1px solid var(--rf-line, #e5e7eb); }
		.rf-menu-out { color:var(--rf-red-ink); }
		/* The line dialog's actions (D-275): outlined in their colour, filled when in force. */
		.rf-line-acts { display:flex; flex-direction:column; gap:8px; margin-bottom:16px; padding-bottom:16px; border-bottom:1px solid var(--rf-line); }
		.rf-la { width:100%; justify-content:flex-start; min-height:48px; border-width:2px; }
		.rf-la svg { width:20px; height:20px; flex:none; }
		.rf-la-disc { border-color:var(--rf-acc); color:var(--rf-acc-ink); }
		.rf-la-disc.rf-la-on { background:var(--rf-acc); border-color:var(--rf-acc); color:#fff; }
		.rf-la-free { border-color:var(--rf-green); color:var(--rf-green-ink); }
		.rf-la-free.rf-la-on { background:var(--rf-green); border-color:var(--rf-green); color:#fff; }
		.rf-la-sides { border-color:var(--rf-blue); color:var(--rf-blue-ink); }
		@media (max-width: 575px) { .rf-menu-cols { grid-template-columns:1fr; } }
		.rf-acting [data-act] { opacity:.6; pointer-events:none; }
		.rf-unsent { display:inline-flex; align-self:flex-start; height:22px; align-items:center; padding:0 8px; border-radius:999px; font-size:12px; font-weight:700;
			background:var(--rf-acc-soft); color:var(--rf-acc-ink); }
		.rf-orow.bill { background:var(--rf-amber-soft); }
		.rf-pack { display:flex; flex-wrap:wrap; align-items:center; gap:6px; padding:4px 0; }
		.rf-packi { display:inline-flex; align-items:center; gap:4px; }
		.rf-onote { padding:8px 14px; background:var(--rf-amber-soft); color:var(--rf-amber-ink); font-size:14px; border-bottom:1px solid var(--rf-line2); white-space:pre-wrap; }
		.rf-fire { width:100%; border-color:var(--rf-amber); color:var(--rf-amber-ink); }
		html[dir="ltr"] .rf-root .rf-back svg { transform:scaleX(-1); }

		/* the order */
		.rf-order { flex-direction:column; height:calc(100vh - var(--navbar-height, 48px)); height:calc(100dvh - var(--navbar-height, 48px)); }
		.rf-ohead { flex-shrink:0; }
		.rf-otitle { display:flex; flex-direction:column; line-height:1.3; }
		.rf-otitle strong { font-size:18px; }
		.rf-custchip { min-height:48px; display:flex; align-items:center; gap:4px; padding-block:0; padding-inline:6px 4px; border:1px solid var(--rf-line); border-radius:12px; background:var(--rf-card); }
		.rf-custtxt { display:flex; flex-direction:column; padding:0 6px; min-width:110px; line-height:1.3; font-size:14px; }
		.rf-ocols { flex:1; min-height:0; display:flex; }
		.rf-menu { flex:1; min-width:0; display:flex; flex-direction:column; gap:14px; padding:16px 20px; }
		.rf-srow { display:flex; gap:10px; align-items:center; }
		.rf-sbox { flex:1; height:48px; display:flex; align-items:center; gap:10px; padding:0 14px; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; margin:0; }
		.rf-sbox svg { color:var(--rf-muted); }
		.rf-sbox input { flex:1; border:0; outline:0; font-size:15px; background:transparent; color:var(--rf-ink); min-width:0; }
		.rf-sbox kbd { height:26px; padding:0 8px; border-radius:6px; background:var(--rf-soft); color:var(--rf-ink2); font-size:12px; font-weight:700; display:flex; align-items:center; box-shadow:none; }
		.rf-scan { width:48px; height:48px; }
		.rf-groups { display:flex; gap:8px; flex-wrap:wrap; flex-shrink:0; max-height:104px; overflow-y:auto; }
		.rf-chip { min-height:44px; padding:0 14px; border-radius:999px; border:1px solid var(--rf-line); background:var(--rf-card); color:var(--rf-ink2); font-weight:700; font-size:14px;
			display:inline-flex; align-items:center; cursor:pointer; white-space:nowrap; }
		.rf-chip.on { background:var(--rf-ink); border-color:var(--rf-ink); color:var(--rf-card); }
		.rf-items { flex:1; min-height:0; overflow:auto; display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:12px; align-content:start; }
		.rf-item { position:relative; border:1px solid var(--rf-line); background:var(--rf-card); border-radius:12px; padding:10px; display:flex; flex-direction:column; gap:8px;
			cursor:pointer; text-align:start; color:var(--rf-ink); }
		.rf-item:active { transform:scale(.97); }
		.rf-item img, .rf-ph { width:100%; height:58px; border-radius:8px; object-fit:cover; }
		.rf-ph { display:flex; align-items:center; justify-content:center; font-weight:800; font-size:20px; }
		.rf-ph.c0 { background:var(--rf-acc-soft); color:var(--rf-acc-ink); } .rf-ph.c1 { background:var(--rf-purple-soft); color:var(--rf-purple-ink); }
		.rf-ph.c2 { background:var(--rf-blue-soft); color:var(--rf-blue-ink); } .rf-ph.c3 { background:var(--rf-amber-soft); color:var(--rf-amber-ink); }
		.rf-ph.c4 { background:var(--rf-green-soft); color:var(--rf-green-ink); } .rf-ph.c5 { background:var(--rf-soft); color:var(--rf-ink2); }
		.rf-iname { font-size:14px; font-weight:700; line-height:20px; min-height:40px; }
		.rf-irate { font-size:15px; font-weight:800; }
		.rf-bdg { position:absolute; top:8px; inset-inline-start:8px; min-width:26px; height:26px; border-radius:999px; background:var(--rf-acc); color:#fff; font-size:13px; font-weight:800;
			display:flex; align-items:center; justify-content:center; padding:0 6px; }
		.rf-cart { width:430px; flex-shrink:0; background:var(--rf-card); border-inline-start:1px solid var(--rf-line); display:flex; flex-direction:column; min-height:0; }
		.rf-chead { min-height:52px; flex-shrink:0; display:flex; align-items:center; gap:8px; padding:0 14px; border-bottom:1px solid var(--rf-line); font-size:16px; }
		.rf-ccount { font-size:13px; color:var(--rf-muted); }
		.rf-lines { flex:1; min-height:0; overflow:auto; }
		.rf-ln { display:flex; flex-direction:column; gap:6px; padding:8px 14px; border-bottom:1px solid var(--rf-line2); }
		.rf-ln.fresh { background:var(--rf-acc-tint); }
		.rf-ln.void { opacity:.5; } .rf-ln.void .rf-lnm b, .rf-ln.void .rf-lamt { text-decoration:line-through; }
		.rf-ln.pending { opacity:.75; }
		.rf-ln1, .rf-ln2 { display:flex; align-items:center; gap:8px; }
		.rf-ln1 { align-items:flex-start; }
		.rf-lnm { flex:1; display:flex; flex-direction:column; gap:2px; font-size:15px; }
		.rf-lnm small { font-size:13px; color:var(--rf-muted); }
		.rf-lamt { font-size:15px; white-space:nowrap; }
		.rf-tag { height:24px; padding:0 8px; border-radius:999px; font-size:12px; font-weight:700; display:inline-flex; align-items:center; gap:4px; white-space:nowrap;
			background:var(--rf-blue-soft); color:var(--rf-blue-ink); }
		.rf-tag svg { width:12px; height:12px; }
		.rf-tag.t-new { background:var(--rf-acc-soft); color:var(--rf-acc-ink); }
		.rf-tag.t-saving { background:var(--rf-soft); color:var(--rf-ink2); } .rf-tag.t-saving svg { animation:rf-spin 1s linear infinite; }
		.rf-tag.t-Ready, .rf-tag.t-Served { background:var(--rf-green-soft); color:var(--rf-green-ink); }
		.rf-tag.t-Void { background:var(--rf-red-soft); color:var(--rf-red-ink); }
		.rf-kprog { display:flex; flex-wrap:wrap; gap:4px; } .rf-kprog .rf-tag { height:22px; }
		.rf-radios { display:flex; flex-direction:column; gap:6px; margin-bottom:8px; }
		.rf-radio { display:flex; align-items:center; gap:10px; min-height:44px; padding:0 12px; border:1px solid var(--border-color); border-radius:10px; cursor:pointer; margin:0; font-weight:600; }
		.rf-radio input { width:18px; height:18px; margin:0; }
		.rf-radio:has(input:checked) { border-color:var(--primary); background:var(--control-bg); }
		.rf-tag.t-bill { background:var(--rf-amber-soft); color:var(--rf-amber-ink); vertical-align:middle; }
		@keyframes rf-spin { to { transform:rotate(360deg); } }
		.rf-busy svg { animation:rf-spin 1s linear infinite; } .rf-btn.rf-busy:disabled { opacity:.85; cursor:progress; }
		.rf-hint { font-size:13px; color:var(--rf-muted); }
		.rf-stp { display:flex; align-items:center; border:1px solid var(--rf-line); border-radius:10px; overflow:hidden; height:44px; }
		.rf-stp button { width:44px; height:44px; border:0; background:var(--rf-card); color:var(--rf-ink); cursor:pointer; display:flex; align-items:center; justify-content:center; padding:0; }
		.rf-stp span { min-width:36px; text-align:center; font-weight:800; font-size:15px; }
		.rf-del { color:var(--rf-red); }
		.rf-cfoot { flex-shrink:0; border-top:1px solid var(--rf-line); padding:12px 14px; display:flex; flex-direction:column; gap:10px; }
		.rf-totals { display:flex; flex-direction:column; gap:4px; font-size:14px; color:var(--rf-ink2); }
		.rf-trow { display:flex; }
		.rf-grand { display:flex; align-items:baseline; padding:10px 12px; border-radius:12px; background:var(--rf-bg); color:var(--rf-ink); margin-top:4px; font-size:16px; }
		.rf-gv { font-size:26px; }
		.rf-cbtns { display:flex; flex-direction:column; gap:8px; }
		.rf-c2 { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:8px; }
		@media (max-width: 1100px) {
			.rf-cart { width:360px; } .rf-side { width:280px; } .rf-hide-sm { display:none !important; }
			.rf-items { grid-template-columns:repeat(auto-fill, minmax(130px, 1fr)); }
		}
		@media (max-width: 820px) {
			.rf-main { flex-direction:column; } .rf-side { width:100%; }
			.rf-order { height:auto; } .rf-ocols { flex-direction:column; } .rf-cart { width:100%; border-inline-start:0; border-top:1px solid var(--rf-line); }
			.rf-items { max-height:55vh; } .rf-grid { grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)) !important; }
			.rf-grid .rf-tbl { grid-row:auto !important; grid-column:auto !important; }
		}

		/* payment */
		.rf-root.rf-pay { min-height:0; background:transparent; display:flex; gap:0; margin:-4px; }
		.rf-pbill { width:380px; flex-shrink:0; background:var(--rf-bg); border-inline-end:1px solid var(--rf-line); display:flex; flex-direction:column; padding:16px; gap:12px; border-radius:12px; }
		.rf-pcust { display:flex; align-items:center; gap:10px; min-height:48px; padding:0 12px; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; }
		.rf-plines { flex:1; min-height:200px; max-height:46vh; overflow:auto; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; display:flex; flex-direction:column; font-size:14px; }
		.rf-prow { display:flex; padding:10px 12px; border-bottom:1px solid var(--rf-line2); gap:8px; }
		.rf-ptot { padding:10px 12px; border-top:1px solid var(--rf-line); }
		.rf-ptiles { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; }
		.rf-ptile { border-radius:12px; padding:12px; display:flex; flex-direction:column; gap:2px; font-size:13px; }
		.rf-ptile b { font-size:24px; }
		.rf-ptile.b { background:var(--rf-blue-soft); color:var(--rf-blue-ink); }
		.rf-ptile.g { background:var(--rf-green-soft); color:var(--rf-green-ink); }
		.rf-ptile.r { background:var(--rf-red-soft); color:var(--rf-red-ink); }
		.rf-pkeys { flex:1; min-width:0; display:flex; flex-direction:column; gap:14px; padding:16px 20px; }
		.rf-pmodes { display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr)); gap:10px; }
		.rf-mth { min-height:64px; border-radius:12px; border:2px solid var(--rf-line); background:var(--rf-card); color:var(--rf-ink); font-weight:800; font-size:15px;
			display:flex; flex-direction:column; align-items:center; justify-content:center; gap:2px; cursor:pointer; }
		.rf-mth small { font-size:13px; color:var(--rf-acc-ink); min-height:16px; }
		.rf-mth.on { border-color:var(--rf-acc); background:var(--rf-acc-soft); }
		.rf-plabel { font-size:13px; display:block; margin-bottom:6px; }
		.rf-quicks { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:10px; }
		.rf-qa { min-height:48px; border-radius:10px; border:1px solid var(--rf-line); background:var(--rf-card); color:var(--rf-ink); font-weight:800; font-size:15px; cursor:pointer; }
		.rf-qa.exact { border-color:var(--rf-acc); background:var(--rf-acc-soft); color:var(--rf-acc-ink); }
		.rf-pinput { display:flex; flex-direction:column; gap:10px; }
		.rf-pentry { height:56px; border-radius:10px; background:var(--rf-bg); border:1px solid var(--rf-line); display:flex; align-items:center; padding:0 16px; gap:8px; }
		.rf-pentry .rf-pmode { font-size:14px; color:var(--rf-muted); } .rf-pval { font-size:26px; }
		.rf-pad { display:flex; gap:14px; }
		.rf-keys { flex:1; display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:8px; }
		.rf-kside { width:170px; display:flex; flex-direction:column; gap:8px; }
		.rf-k { min-height:52px; border-radius:10px; border:1px solid var(--rf-line); background:var(--rf-card); color:var(--rf-ink); font-size:22px; font-weight:800; cursor:pointer; }
		.rf-k:active, .rf-qa:active, .rf-mth:active { transform:scale(.97); }
		.rf-kdel { color:var(--rf-red); } .rf-kclr { font-size:17px; color:var(--rf-acc-ink); }
		.rf-kadd { flex:1; background:var(--rf-ink); border-color:var(--rf-ink); color:var(--rf-card); font-size:18px; }
		.rf-added { display:flex; flex-direction:column; gap:6px; font-size:14px; }
		.rf-added:not(:empty) { padding:10px; border:1px solid var(--rf-line); border-radius:10px; }
		.rf-onacc { display:flex; align-items:center; gap:8px; font-weight:700; margin:0; }
		.rf-pnote:empty { display:none; }
		.rf-pacts { display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1.4fr); gap:10px; }
		.rf-pacts .rf-btn { min-height:56px; }
		.rf-pacts kbd { height:24px; padding:0 8px; border-radius:6px; background:var(--rf-soft); color:var(--rf-ink2); font-size:12px; display:flex; align-items:center; box-shadow:none; }
		.rf-pdone { font-size:17px; }
		.rf-psplit { align-self:flex-start; }
		.rf-paidmsg { font-size:16px; display:flex; flex-direction:column; gap:8px; }
		.rf-paidchg { font-size:22px; }
		@media (max-width: 900px) { .rf-root.rf-pay { flex-direction:column; } .rf-pbill { width:auto; } .rf-quicks { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
		/* invoices (D-215) */
		.rf-inv { flex:1; min-height:0; display:flex; gap:16px; padding:0 16px 16px; }
		.rf-inv-l { flex:1.15; min-width:0; display:flex; flex-direction:column; gap:10px; }
		.rf-inv-d { flex:1; min-width:0; overflow:auto; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:14px; padding:16px; display:flex; flex-direction:column; gap:12px; }
		.rf-inv-tabs { flex-wrap:wrap; }
		.rf-inv-kpi { display:flex; gap:10px; } .rf-inv-kpi:empty { display:none; }
		.rf-kpi { flex:1; background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; padding:10px 14px; display:flex; flex-direction:column; gap:4px; }
		.rf-kpi span { font-size:13px; color:var(--rf-muted); } .rf-kpi b { font-size:20px; }
		.rf-chips { display:flex; gap:8px; flex-wrap:wrap; } .rf-chips:empty { display:none; }
		.rf-inv-list { flex:1; min-height:0; overflow:auto; display:flex; flex-direction:column; gap:8px; }
		.rf-inv-l > * { flex-shrink:0; } .rf-inv-l > .rf-inv-list { flex:1 1 0; min-height:120px; } .rf-inv-l .rf-sbox { flex:none; }
		.rf-invr { flex-shrink:0; }
		.rf-invr { all:unset; box-sizing:border-box; cursor:pointer; display:flex; flex-direction:column; gap:6px; padding:12px 14px; min-height:44px;
			background:var(--rf-card); border:1px solid var(--rf-line); border-radius:12px; }
		.rf-invr.on { border-color:var(--rf-acc); box-shadow:0 0 0 1px var(--rf-acc); }
		.rf-invr .rf-orow1 .rf-muted { font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }
		.rf-inv-more { align-self:center; }
		.rf-invd-h { display:flex; align-items:center; gap:10px; flex-wrap:wrap; } .rf-invd-n { font-size:20px; }
		.rf-invd-t { width:100%; border-collapse:collapse; font-size:14px; }
		.rf-invd-t th { text-align:start; color:var(--rf-muted); font-weight:700; padding:6px 4px; border-bottom:1px solid var(--rf-line); }
		.rf-invd-t td { padding:8px 4px; border-bottom:1px solid var(--rf-line2); }
		.rf-invd-t th:not(:first-child), .rf-invd-t td:not(:first-child) { text-align:end; white-space:nowrap; }
		.rf-tots { display:flex; flex-direction:column; gap:6px; padding-top:8px; border-top:1px solid var(--rf-line); }
		.rf-tbig { font-size:18px; }
		.rf-here.on { border-color:var(--rf-green); color:var(--rf-green-ink); background:var(--rf-green-soft); }
		.rf-st.k-ok { background:var(--rf-green-soft); color:var(--rf-green-ink); }
		.rf-st.k-warn { background:var(--rf-amber-soft); color:var(--rf-amber-ink); }
		.rf-st.k-void { background:var(--rf-red-soft); color:var(--rf-red-ink); }
		@media (max-width:900px) { .rf-inv { flex-direction:column; overflow:auto; } .rf-inv-l, .rf-inv-d { flex:none; } .rf-inv-list { max-height:60vh; } }
`;
	}
}
