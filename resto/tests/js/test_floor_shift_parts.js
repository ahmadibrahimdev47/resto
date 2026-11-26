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
	assert.ok(html.includes("2 advances · taken off salaries, not an expense"));
	assert.ok(html.includes("Collected − Expenses − Purchases − Advances"));
	assert.ok(html.includes("Ali"));
	t.done();
});
