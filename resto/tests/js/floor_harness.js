// Small stand-ins for Frappe and the browser, to run the real floor page script in Node.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "../../resto/page/resto_floor/resto_floor.js"), "utf8");
const tick = () => new Promise((r) => setTimeout(r, 5));

// server(): what the server answers the next call: "ok", "refuse", "silent" (no answer), or an
// HTTP status of a failure that is not a refusal (0: no answer at all).
function load(server, printer = () => true) {
	const store = new Map();
	const seen = { calls: [], msgs: [], alerts: [], dialogs: [], printed: [] };
	class Dialog {
		constructor(opts) {
			this.opts = opts;
			seen.dialogs.push(this);
		}
		show() {
			// As in Frappe: display is set once the window has opened, not at once.
			setTimeout(() => (this.display = true), 0);
		}
		hide() {
			this.display = false;
			if (this.opts.on_hide) this.opts.on_hide();
		}
	}
	const chain = new Proxy(function () {}, {
		get: (t, p) => (p === "length" ? 0 : chain),
		apply: () => chain,
	});
	const ctx = {
		console,
		setTimeout,
		clearTimeout,
		Promise,
		JSON,
		Math,
		Date,
		Set,
		Object,
		Array,
		String,
		window: { addEventListener: () => {} },
		document: { dir: "rtl" },
		resto: { print_html: (html) => (seen.printed.push(html), Promise.resolve(printer())) },
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, String(v)),
			removeItem: (k) => store.delete(k),
		},
		__: (s, args) => String(s).replace(/\{(\d+)\}/g, (m, i) => (args && args[i] !== undefined ? args[i] : m)),
		cint: (v) => {
			const n = parseInt(v, 10);
			return isNaN(n) ? 0 : n;
		},
		flt: (v) => parseFloat(v) || 0,
		format_currency: (v) => String(v),
		$: chain,
		frappe: {
			pages: { "resto-floor": {} },
			session: { user: "waiter@example.com" },
			utils: {
				get_random: (n) => Math.random().toString(36).slice(2, 2 + n).padEnd(n, "x"),
				escape_html: (s) => String(s),
				play_sound: () => {},
			},
			ui: { Dialog },
			realtime: { on: () => {} },
			datetime: { now_datetime: () => "2026-11-25 20:00:00", str_to_user: (s) => s },
			msgprint: (m) => seen.msgs.push(typeof m === "string" ? m : m.message),
			show_alert: (m) => seen.alerts.push(m.message || m),
			call: (opts) => {
				seen.calls.push({ method: opts.method.split(".").pop(), args: opts.args });
				const req = { status: 200, responseJSON: {} };
				const answer = server();
				setTimeout(() => {
					if (answer === "ok" || (answer && typeof answer === "object")) {
						opts.callback({ message: answer === "ok" ? null : answer.message });
						opts.always && opts.always();
					} else if (answer === "refuse") {
						req.status = 417;
						req.responseJSON = { exc_type: "ValidationError", _server_messages: "[]" };
						opts.error && opts.error();
					} else if (answer !== "silent") {
						req.status = answer;
						opts.error && opts.error();
					}
				}, 0);
				return req;
			},
		},
	};
	vm.createContext(ctx);
	vm.runInContext(`${SOURCE}\nthis.__t = { RestoFloor, outbox, set_outbox, round_money };`, ctx);
	const { RestoFloor, outbox, set_outbox, round_money } = ctx.__t;
	const floor = Object.create(RestoFloor.prototype);
	Object.assign(floor, { page: { body: {} }, wrapper: {}, order: null, floor: null, offline: false, _gen: 0 });
	floor.reload_lines = () => Promise.resolve();
	floor.show_view = () => {};
	floor.render_lines = () => {};
	// The test is over: timers stop, and a send still on its way retries nothing.
	const done = () => {
		clearTimeout(floor._retry);
		clearTimeout(floor._tap_timer);
		floor.drain = () => Promise.resolve();
	};
	return { floor, outbox, set_outbox, round_money, seen, done };
}


module.exports = { load, tick };