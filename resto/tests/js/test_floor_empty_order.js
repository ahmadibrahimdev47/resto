// An order open on a table with no dish on it says so on the tile (D-296), so it is not left
// holding the table: the cashier adds dishes or cancels it.
//   node apps/resto/resto/tests/js/test_floor_empty_order.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("./floor_harness");

const tile = (order) => ({ name: "T2", seats: 4, status: "occupied", order: { name: "ORD-1", guests: 2, minutes: 1200, net_total: 0, ...order } });

test("an empty order on a table is marked; one with dishes is not", () => {
	const t = load(() => "ok");
	const f = t.floor;
	f.kitchen_progress = () => "";
	assert.match(f.table_card(tile({ dishes: 0, unsent_count: 0 })), /rf-tempty/);
	assert.doesNotMatch(f.table_card(tile({ dishes: 2, unsent_count: 0 })), /rf-tempty/);
	assert.doesNotMatch(f.table_card(tile({ dishes: 1, unsent_count: 1 })), /rf-tempty/);
	t.done();
});
