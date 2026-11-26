// A table or order card shows where its sent dishes are (D-267): at the kitchen, being made and
// ready, by dish count; the parts at zero are left out.
//   node apps/resto/resto/tests/js/test_floor_kitchen_progress.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("./floor_harness");

const parts = (html) => [...html.matchAll(/<span class="rf-tag t-(\w+)">([^<]*)<\/span>/g)].map((m) => [m[1], m[2]]);

test("each part with its dish count, in the order's tag colors", () => {
	const t = load(() => "ok");
	const html = t.floor.kitchen_progress({ sent_qty: 2, progress_qty: 1, ready_qty: 1.5 });
	assert.deepEqual(parts(html), [
		["Sent", "In the Kitchen 2"],
		["InProgress", "In Progress 1"],
		["Ready", "Ready 1.5"],
	]);
	t.done();
});

test("parts at zero are left out; nothing sent, nothing shown", () => {
	const t = load(() => "ok");
	assert.deepEqual(parts(t.floor.kitchen_progress({ sent_qty: 0, progress_qty: 3, ready_qty: 0 })), [["InProgress", "In Progress 3"]]);
	assert.equal(t.floor.kitchen_progress({ sent_qty: 0, progress_qty: 0, ready_qty: 0 }), "");
	assert.equal(t.floor.kitchen_progress({}), ""); // an order list from before (offline copy)
	t.done();
});

test("the table card and the order row carry it", () => {
	const t = load(() => "ok");
	const o = { name: "RO-1", order_type: "Takeaway", dishes: 4, minutes: 5, net_total: 100, sent_qty: 1, ready_qty: 2 };
	const card = t.floor.table_card({ name: "T1", status: "occupied", seats: 4, order: o });
	assert.deepEqual(parts(card), [
		["Sent", "In the Kitchen 1"],
		["Ready", "Ready 2"],
	]);
	assert.deepEqual(parts(t.floor.order_row(o)), [
		["Sent", "In the Kitchen 1"],
		["Ready", "Ready 2"],
	]);
	t.done();
});
