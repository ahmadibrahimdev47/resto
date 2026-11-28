// The order's kitchen state as the floor's buttons show it (D-293): its least advanced sent dish;
// marked served, served. Not sent and voided dishes do not count.
//   node apps/resto/resto/tests/js/test_floor_kitchen_state.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("./floor_harness");

const order = (statuses, extra = {}) => ({ items: statuses.map((kitchen_status) => ({ kitchen_status })), ...extra });

test("the least advanced sent dish is the order's state", () => {
	const t = load(() => "ok");
	const f = t.floor;
	assert.equal(f.kitchen_state(order(["Served", "Ready", "In Progress"])), "In Progress");
	assert.equal(f.kitchen_state(order(["Ready", "Served", "Void", "Not Sent"])), "Ready");
	assert.equal(f.kitchen_state(order(["Served", "Served"])), "Served");
	assert.equal(f.kitchen_state(order(["Sent", "Served"])), "Sent");
	t.done();
});

test("nothing sent: no state; marked served: served", () => {
	const t = load(() => "ok");
	assert.equal(t.floor.kitchen_state(order(["Not Sent", "Void"])), null);
	assert.equal(t.floor.kitchen_state(order(["Sent"], { served_marked_at: "2026-11-28 13:00:00" })), "Served");
	t.done();
});
