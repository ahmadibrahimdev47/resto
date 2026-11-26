// The close dialog shows how each payment method's expected amount is made (D-236), from the
// parts the server worked out; counting blind, nothing of it before the shift is closed.
//   node apps/resto/resto/tests/js/test_floor_shift_parts.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("./floor_harness");

const CASH = {
	mode_of_payment: "Cash",
	opening_amount: 500,
	sales_amount: 405,
	debts_amount: 0,
	expenses_amount: 20,
	purchases_amount: 60,
	refunds_amount: 0,
	expected_amount: 825,
};
const CARD = { mode_of_payment: "InstaPay", opening_amount: 0, sales_amount: 100, debts_amount: 0, expenses_amount: 0, purchases_amount: 0, refunds_amount: 0, expected_amount: 100 };

const lines = (html, mode) => {
	const part = html.split(`<summary>${mode}</summary>`)[1].split("</details>")[0];
	return [...part.matchAll(/<span>([^<]*)<\/span><span class="rf-amt">([^<]*)<\/span>/g)].map((m) => [m[1], m[2]]);
};

test("each method's expected amount is broken down, the parts that are there", () => {
	const t = load(() => "ok");
	const html = t.floor.shift_report({ summary: {}, rows: [CASH, CARD] }, true);
	assert.deepEqual(lines(html, "Cash"), [
		["Opening (starting change)", "+500"],
		["Sales paid with this method", "+405"],
		["Drawer Running Expenses", "−20"],
		["Drawer Stock Purchases", "−60"],
		["= Expected", "825"],
	]);
	assert.deepEqual(lines(html, "InstaPay"), [
		["Opening (starting change)", "+0"],
		["Sales paid with this method", "+100"],
		["= Expected", "100"],
	]);
	t.done();
});

test("debts and refunds show when there are any", () => {
	const t = load(() => "ok");
	const html = t.floor.shift_report({ summary: {}, rows: [{ ...CASH, debts_amount: 50, refunds_amount: 30, expected_amount: 845 }] }, true);
	const got = lines(html, "Cash");
	assert.deepEqual(got[2], ["Debts collected", "+50"]);
	assert.deepEqual(got[5], ["Paid back to customers", "−30"]);
	t.done();
});

test("counting blind: no breakdown before the shift is closed, as no expected amount", () => {
	const t = load(() => "ok");
	const html = t.floor.shift_report({ blind: true, rows: [{ mode_of_payment: "Cash" }] }, true);
	assert.ok(!html.includes("rf-xparts"));
	assert.ok(!html.includes("Expected"));
	// Once closed, the report has its figures: the breakdown shows.
	const closed = t.floor.shift_report({ summary: {}, rows: [{ ...CASH, closing_amount: 825, difference: 0 }] }, false);
	assert.ok(closed.includes("rf-xparts"));
	t.done();
});

test("staff advances: a line of their own in the breakdown and a box of their own (D-237)", () => {
	const t = load(() => "ok");
	const row = { ...CASH, advances_amount: 100, expected_amount: 725 };
	const html = t.floor.shift_report(
		{ summary: { advances_count: 2, advances_total: 100, collected: 405, net_cash: 305 }, rows: [row], advances: [{ employee: "E1", employee_name: "Ali", amount: 100 }] },
		true
	);
	assert.deepEqual(lines(html, "Cash")[4], ["Staff Advances", "−100"]);
	assert.ok(html.includes("2 advances · owed by the workers, not an expense"));
	assert.ok(html.includes("Collected − Advances"));
	assert.ok(html.includes("Ali"));
	t.done();
});

test("cash into the drawer and drops to the main safe: their own lines, boxes and net (D-245)", () => {
	const t = load(() => "ok");
	const row = { ...CASH, cash_in_amount: 80, drops_amount: 300, expected_amount: 605 };
	const html = t.floor.shift_report(
		{
			summary: { cash_in_count: 2, cash_in_total: 80, drops_count: 1, drops_total: 300, expenses_count: 1, collected: 405, net_cash: 165 },
			rows: [row],
			cash_moves: [
				{ move: "Cash In", reason: "Staff Advance Repaid", employee_name: "Ali", amount: 50 },
				{ move: "Cash In", reason: "Change from the Main Safe", amount: 30 },
				{ move: "Cash Drop", reason: "To the Main Safe", remarks: "noon", amount: 300 },
			],
		},
		true
	);
	const got = lines(html, "Cash");
	assert.deepEqual(got[4], ["Cash In to the Drawer", "+80"]);
	assert.deepEqual(got[5], ["Cash Drops to the Main Safe", "−300"]);
	assert.deepEqual(got[6], ["= Expected", "605"]);
	assert.ok(html.includes("2 times · advances paid back, change and the like, not sales"));
	assert.ok(html.includes("1 drops · moved to the safe, not spent"));
	assert.ok(html.includes("Collected − Expenses + Cash In − Drops"));
	assert.ok(html.includes("Cash In and Drops"));
	assert.ok(html.includes("Staff Advance Repaid: Ali"));
	assert.ok(html.includes("noon"));
	t.done();
});

test("the shift summary now: opening and expected, no count and no difference (D-262)", () => {
	const t = load(() => "ok");
	const html = t.floor.shift_report({ live: 1, as_of: "2026-11-25 20:00:00", summary: {}, rows: [CASH, CARD], open_orders: 2 }, false);
	assert.ok(html.includes("Shift open · as of 2026-11-25 20:00:00"));
	assert.ok(html.includes("2 orders are still open: not in these figures until they are paid."));
	// Each header over its column: the numbers sit at the end of the column, and so do their headers (D-280).
	assert.ok(html.includes('<th class="rf-amt">Opening</th><th class="rf-amt">Expected</th>'));
	assert.ok(html.includes('<table class="table table-condensed rf-recon">'));
	assert.ok(!html.includes("Counted"));
	assert.ok(!html.includes("Difference"));
	assert.ok(!html.includes("<input"));
	assert.ok(html.includes("925"), "the total expected");
	// Counting blind (a cashier): no figure at all, a note instead.
	const blind = t.floor.shift_report({ live: 1, as_of: "2026-11-25 20:00:00", blind: true, rows: [{ mode_of_payment: "Cash" }] }, false);
	assert.ok(!blind.includes("Expected"));
	assert.ok(!blind.includes("Payment Reconciliation"));
	assert.ok(blind.includes("shown to the manager only"));
	t.done();
});
