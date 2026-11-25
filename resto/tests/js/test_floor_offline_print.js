// The kitchen ticket and the bill never wait for the connection (D-219): printed on the device,
// the bill's total worked out as the server does.
//   node apps/resto/resto/tests/js/test_floor_offline_print.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("./floor_harness");

const VAT = { description: "VAT 14%", charge_type: "On Net Total", rate: 14, account_head: "VAT - X" };
const rules = (extra) => ({ taxes: [VAT], service_account: "Service - X", vat_on_service: 1, disable_rounded_total: 0, smallest_fraction: 0, ...extra });

function order(extra) {
	return {
		name: "ORD-9",
		order_type: "Dine In",
		resto_table: "T1",
		discount_amount: 0,
		service_charge_percent: 0,
		delivery_fee: 0,
		invoiced_total: 0,
		net_total: 100,
		items: [
			{ name: "L1", item_code: "SOUP", item_name: "Soup", qty: 2, rate: 30, amount: 60, kitchen_status: "Not Sent", billed_qty: 0 },
			{ name: "L2", item_code: "RICE", item_name: "Rice", qty: 1, rate: 40, amount: 40, kitchen_status: "Sent", billed_qty: 0, notes: "no salt" },
			{ name: "L3", item_code: "TEA", item_name: "Tea", qty: 1, rate: 10, amount: 10, kitchen_status: "Void", billed_qty: 0 },
		],
		...extra,
	};
}

function floor_with(server, o, bill_rules) {
	const t = load(server);
	t.floor.order = o;
	t.floor.floor = { bill_rules, brand: "Test Kitchen", user_name: "Cashier" };
	return t;
}

test("offline: the kitchen ticket prints here and is recorded later, only for what was printed", async () => {
	const t = floor_with(() => 0, order(), rules());
	t.floor.offline = true;
	t.set_outbox([{ op: "a1", method: "add_items", args: { order: "ORD-9", items: [] }, label: "2 × Soup", at: Date.now() - 600000 }]);
	await t.floor.send_or_bill("send");
	t.done();
	assert.equal(t.seen.printed.length, 1);
	assert.match(t.seen.printed[0], /2 × Soup/);
	assert.doesNotMatch(t.seen.printed[0], /Rice/, "already sent");
	const jobs = t.outbox();
	assert.equal(jobs.length, 2);
	assert.equal(jobs[0].ok, 1, "the dishes printed for the kitchen are sent, not offered for discarding");
	assert.equal(jobs[1].method, "send_to_kitchen");
	assert.deepEqual(JSON.parse(JSON.stringify(jobs[1].args.printed)), { SOUP: 2 });
	assert.equal(jobs[1].ok, 1);
	assert.equal(t.floor.order.items[0].kitchen_status, "Sent");
	assert.equal(t.seen.dialogs.length, 0, "no question in the middle of it");
});

test("the server not answering: Send to Kitchen falls back to printing here", async () => {
	const t = floor_with(() => 0, order(), rules());
	await t.floor.send_or_bill("send");
	t.done();
	assert.equal(t.seen.calls[0].method, "send_to_kitchen");
	assert.equal(t.seen.calls[0].args.printed, undefined, "tried through the server first");
	assert.equal(t.seen.printed.length, 1);
	assert.equal(t.outbox().at(-1).method, "send_to_kitchen");
	assert.deepEqual(JSON.parse(JSON.stringify(t.outbox().at(-1).args.printed)), { SOUP: 2 });
});

test("connected and nothing waiting: Send to Kitchen goes through the server, nothing printed here", async () => {
	const t = floor_with(() => ({ message: ["KOT-1"] }), order(), rules());
	t.floor.load_order = () => {};
	await t.floor.send_or_bill("send");
	t.done();
	assert.equal(t.seen.printed.length, 0);
	assert.equal(t.outbox().length, 0);
});

test("a device that cannot print: nothing is recorded as sent", async () => {
	const t = load(() => 0, () => false);
	t.floor.order = order();
	t.floor.floor = { bill_rules: rules() };
	t.floor.offline = true;
	await t.floor.send_or_bill("send");
	t.done();
	assert.equal(t.outbox().length, 0);
	assert.equal(t.floor.order.items[0].kitchen_status, "Not Sent");
	assert.ok(t.seen.msgs.length);
});

test("offline bill: service first and taxed (Charge VAT on Service), then rounded", () => {
	// Food 100, discount 10: net 90; service 12% = 10.80; VAT 14% of 100.80 = 14.11; 114.91 -> 115.
	const t = floor_with(() => 0, order({ discount_amount: 10, service_charge_percent: 12 }), rules());
	const b = t.floor.bill_on_device(t.floor.order);
	t.done();
	assert.equal(b.food, 100);
	assert.equal(b.discount, 10);
	assert.deepEqual(
		Array.from(b.taxes, (x) => x.amount),
		[10.8, 14.11]
	);
	assert.equal(b.grand, 114.91);
	assert.equal(b.payable, 115);
	assert.equal(b.estimate, false);
});

test("offline bill: service last and not taxed", () => {
	// net 90; VAT 12.60; service 10.80: 113.40 -> 113.
	const t = floor_with(() => 0, order({ discount_amount: 10, service_charge_percent: 12 }), rules({ vat_on_service: 0 }));
	const b = t.floor.bill_on_device(t.floor.order);
	t.done();
	assert.deepEqual(
		Array.from(b.taxes, (x) => [x.service, x.amount]),
		[
			[false, 12.6],
			[true, 10.8],
		]
	);
	assert.equal(b.payable, 113);
});

test("offline bill: delivery fee, service on the food only", () => {
	// Food 100 + fee 20: net 120; VAT 16.80; service 10% of 100 = 10 (Actual): 146.80 -> 147.
	const o = order({ order_type: "Delivery", resto_table: null, delivery_fee: 20, service_charge_percent: 10 });
	const t = floor_with(() => 0, o, rules({ vat_on_service: 0 }));
	const b = t.floor.bill_on_device(o);
	t.done();
	assert.equal(b.fee, 20);
	assert.deepEqual(
		Array.from(b.taxes, (x) => x.amount),
		[16.8, 10]
	);
	assert.equal(b.payable, 147);
});

test("offline bill: prices that include tax add nothing; no rounding when it is off", () => {
	const t = floor_with(() => 0, order({ discount_amount: 0.5 }), rules({ taxes: [{ ...VAT, included_in_print_rate: 1 }], disable_rounded_total: 1 }));
	const b = t.floor.bill_on_device(t.floor.order);
	t.done();
	assert.equal(b.taxes.length, 0);
	assert.equal(b.payable, 99.5);
});

test("offline bill: printed here with its lines and total, marked as printed without a connection", async () => {
	const t = floor_with(() => 0, order({ service_charge_percent: 12 }), rules());
	t.floor.offline = true;
	await t.floor.send_or_bill("bill");
	t.done();
	assert.equal(t.seen.printed.length, 1);
	const html = t.seen.printed[0];
	assert.match(html, /Bill, printed without a connection/);
	assert.match(html, /2 × Soup/);
	assert.match(html, /1 × Rice/);
	assert.doesNotMatch(html, /Tea/, "voided");
	assert.match(html, />128</); // 100 + service 12 + VAT 14% of 112 (15.68) = 127.68 -> 128
});

test("rounding as Frappe: to whole units, a half to the even one; or to the currency's fraction", () => {
	const t = load(() => "ok");
	t.done();
	assert.equal(t.round_money(112.5, 0), 112);
	assert.equal(t.round_money(113.5, 0), 114);
	assert.equal(t.round_money(113.49, 0), 113);
	assert.equal(t.round_money(113.37, 0.25), 113.25);
	assert.equal(t.round_money(113.4, 0.25), 113.5);
});
