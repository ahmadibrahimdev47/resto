// The floor's outbox (D-213, D-218): changes kept on the device and sent in order.
// Runs the real page script with small stand-ins for Frappe and the browser:
//   node apps/resto/resto/tests/js/test_floor_outbox.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "../../resto/page/resto_floor/resto_floor.js"), "utf8");
const tick = () => new Promise((r) => setTimeout(r, 5));

// server(): what the server answers the next call: "ok", "refuse", "silent" (no answer), or an
// HTTP status of a failure that is not a refusal (0: no answer at all).
function load(server) {
	const store = new Map();
	const seen = { calls: [], msgs: [], alerts: [], dialogs: [] };
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
			msgprint: (m) => seen.msgs.push(typeof m === "string" ? m : m.message),
			show_alert: (m) => seen.alerts.push(m.message || m),
			call: (opts) => {
				seen.calls.push({ method: opts.method.split(".").pop(), args: opts.args });
				const req = { status: 200, responseJSON: {} };
				const answer = server();
				setTimeout(() => {
					if (answer === "ok") {
						opts.callback({ message: null });
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
	vm.runInContext(`${SOURCE}\nthis.__t = { RestoFloor, outbox, set_outbox };`, ctx);
	const { RestoFloor, outbox, set_outbox } = ctx.__t;
	const floor = Object.create(RestoFloor.prototype);
	Object.assign(floor, { page: { body: {} }, wrapper: {}, order: null, floor: null, offline: false, _gen: 0 });
	floor.reload_lines = () => Promise.resolve();
	floor.show_view = () => {};
	const done = () => {
		clearTimeout(floor._retry);
		clearTimeout(floor._tap_timer);
	};
	return { floor, outbox, set_outbox, seen, done };
}

const answers = (...list) => () => (list.length > 1 ? list.shift() : list[0]);

const job = (extra) => ({
	op: `op${Math.random().toString(36).slice(2, 10)}`,
	method: "update_line",
	args: { order: "ORD-1", line: "L1", step: 1 },
	label: "",
	at: Date.now(),
	...extra,
});
const STALE = () => Date.now() - 5 * 60000;

test("a drain of an empty outbox does not stop the changes made after it", async () => {
	const t = load(() => "ok");
	await t.floor.drain(); // nothing to send (a line's + / - button flushes the taps first)
	await t.floor.sync_call("update_line", { order: "ORD-1", line: "L1", step: 1 });
	t.done();
	assert.deepEqual(
		t.seen.calls.map((c) => c.method),
		["update_line"]
	);
	assert.equal(t.outbox().length, 0);
	assert.equal(t.floor._draining, null);
	assert.equal(await t.floor.settled(), true);
});

test("+ then − on a saved line (the taps flushed first each time) are both sent, in order", async () => {
	const t = load(() => "ok");
	// What a line's + / − button does: flush the taps (none), then queue the step.
	t.floor.flush_taps();
	t.floor.sync_call("update_line", { order: "ORD-1", line: "L1", step: 1 }, "+1 × Water");
	t.floor.flush_taps();
	await t.floor.sync_call("update_line", { order: "ORD-1", line: "L1", step: -1 }, "−1 × Water");
	await t.floor.drain();
	t.done();
	assert.deepEqual(
		t.seen.calls.map((c) => [c.method, c.args.step]),
		[
			["update_line", 1],
			["update_line", -1],
		]
	);
	assert.equal(t.outbox().length, 0);
	assert.equal(await t.floor.settled(), true);
});

test("a change kept more than two minutes waits for the choice, in one window; Send Now sends it", async () => {
	const t = load(() => "ok");
	t.set_outbox([job({ method: "add_items", args: { order: "ORD-1", items: [] }, label: "1 × Soup", at: STALE() })]);
	// An action on the order: the drain asks, and settled() would ask too, in the same moment.
	assert.equal(await t.floor.settled(), false);
	await t.floor.drain();
	await tick();
	assert.equal(t.seen.calls.length, 0);
	assert.equal(t.seen.dialogs.length, 1, "one window");
	const d = t.seen.dialogs[0];
	assert.ok(d.display);
	await d.opts.primary_action();
	t.done();
	assert.deepEqual(
		t.seen.calls.map((c) => c.method),
		["add_items"]
	);
	assert.equal(t.outbox().length, 0);
	assert.ok(t.seen.alerts.includes("Changes sent"));
});

test("a stale change discarded is not sent; closed without a choice it is asked again later", async () => {
	const t = load(() => "ok");
	t.set_outbox([job({ at: STALE() })]);
	await t.floor.drain();
	t.seen.dialogs[0].hide(); // closed with X
	await t.floor.drain(); // a refresh a moment later: not asked again yet
	assert.equal(t.seen.dialogs.length, 1);
	assert.equal(await t.floor.settled(), false); // an action on the order asks at once
	assert.equal(t.seen.dialogs.length, 2);
	t.seen.dialogs[1].opts.secondary_action();
	await t.floor.drain();
	t.done();
	assert.equal(t.seen.calls.length, 0);
	assert.equal(t.outbox().length, 0);
});

test("a server error is not 'not connected': what waits is shown, and it closes once sent again", async () => {
	const t = load(answers(502, "ok"));
	t.set_outbox([job({ label: "+1 × Water" })]);
	assert.equal(await t.floor.settled(), false);
	await tick();
	assert.equal(t.floor.offline, false);
	assert.ok(!t.seen.msgs.some((m) => String(m).startsWith("Not connected")));
	const d = t.seen.dialogs.at(-1);
	assert.ok(d && d.display);
	assert.match(d.opts.fields[0].options, /error 502/);
	assert.match(d.opts.fields[0].options, /\+1 × Water/);
	assert.equal(t.outbox().length, 1, "kept");
	assert.ok(t.floor._retry, "sent again by itself");
	clearTimeout(t.floor._retry);
	await t.floor.drain(); // the retry
	t.done();
	assert.equal(t.outbox().length, 0);
	assert.equal(d.display, false, "the window closes: nothing of it waits any more");
});

test("no answer at all, or a login page: kept, and the screen says not connected", async () => {
	for (const answer of [0, 401]) {
		const t = load(() => answer);
		t.set_outbox([job()]);
		assert.equal(await t.floor.settled(), false);
		t.done();
		assert.equal(t.floor.offline, true, `status ${answer}`);
		assert.ok(t.seen.msgs.some((m) => String(m).startsWith("Not connected")));
		assert.equal(t.outbox().length, 1);
	}
});

test("a server that never answers: the change is kept after the time limit", { timeout: 30000 }, async () => {
	const t = load(() => "silent");
	t.set_outbox([job()]);
	await t.floor.drain();
	t.done();
	assert.equal(t.floor.offline, true);
	assert.equal(t.outbox().length, 1);
});

test("Send Now does not say 'sent' when the server refused the change", async () => {
	const t = load(() => "refuse");
	t.set_outbox([job({ at: STALE() })]);
	await t.floor.drain();
	await t.seen.dialogs[0].opts.primary_action();
	t.done();
	assert.equal(t.outbox().length, 0, "a refused change is dropped");
	assert.ok(!t.seen.alerts.includes("Changes sent"));
});

test("an entry that is not a change of this screen is dropped, the next is sent", async () => {
	const t = load(() => "ok");
	t.set_outbox([null, { op: "x" }, job()]);
	await t.floor.drain();
	t.done();
	assert.equal(t.seen.calls.length, 1);
	assert.equal(t.outbox().length, 0);
});
