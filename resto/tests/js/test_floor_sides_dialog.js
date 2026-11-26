// The sides dialog (D-268): a side group of one portion per dish is a choice of one option or
// «without»; more portions are whole counts; what is saved is only what changed.
//   node apps/resto/resto/tests/js/test_floor_sides_dialog.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("./floor_harness");

// What get_sides gives for one dish of a recipe serving 200 g of vermicelli rice, in a group of
// max 1 whose options are 0.2 Kg a portion: one portion of the recipe's rice, none of the other.
const RICE = {
	key: "Rice",
	label: "Rice",
	max: 1,
	free: 0,
	single: 0,
	shared: false,
	options: [
		{ item: "RICE-VERM", item_name: "Vermicelli Rice", total: 1, price: 0, default: 1 },
		{ item: "RICE-BASM", item_name: "Basmati Rice", total: 0, price: 0, default: 0 },
	],
};
const one_dish = (slot) => ({ per_dish: 1, qty: 2, title: "Sausage × 2", slots: [slot] });
const checked = (html) => [...html.matchAll(/<input type="radio" name="rfs_0" value="([^"]*)" checked>/g)].map((m) => m[1]);

test("max 1: one portion of the recipe's side, as a choice with «without», not 5", () => {
	const t = load(() => "ok");
	const fields = t.floor.sides_fields(one_dish(RICE), () => {});
	const radios = fields.find((f) => f.fieldname === "r_0");
	assert.ok(radios, "the slot is a choice");
	assert.ok(!fields.some((f) => /^s_0_/.test(f.fieldname || "")), "no number inputs");
	assert.deepEqual(checked(radios.options), ["0"]); // vermicelli, the recipe's own
	assert.ok(radios.options.includes(">Without<"));
	assert.ok(!radios.options.includes("5"));
	t.done();
});

test("max 1: «without» saves the slot empty; the same choice saves nothing; the other option swaps", () => {
	const t = load(() => "ok");
	const r = one_dish(RICE);
	const pick = (p) => t.floor.sides_choice(r, t.floor.sides_values(r, () => 0, () => p));
	assert.deepEqual(JSON.parse(JSON.stringify(pick(""))), { Rice: {} });
	assert.deepEqual(JSON.parse(JSON.stringify(pick("0"))), {});
	assert.deepEqual(JSON.parse(JSON.stringify(pick("1"))), { Rice: { "RICE-BASM": 1 } });
	t.done();
});

test("max 1 and nothing served now: «without» is the one chosen", () => {
	const t = load(() => "ok");
	const none = { ...RICE, options: RICE.options.map((o) => ({ ...o, total: 0 })) };
	const radios = t.floor.sides_fields(one_dish(none), () => {}).find((f) => f.fieldname === "r_0");
	assert.deepEqual(checked(radios.options), [""]);
	t.done();
});

test("more than one portion: whole counts per option; shared or several dishes stay decimal", () => {
	const t = load(() => "ok");
	const two = { ...RICE, max: 2 };
	const f = t.floor.sides_fields(one_dish(two), () => {}).filter((x) => /^s_0_/.test(x.fieldname || ""));
	assert.deepEqual(JSON.parse(JSON.stringify(f.map((x) => [x.fieldtype, x.default]))), [["Int", 1], ["Int", 0]]);
	const shared = { ...two, shared: true, options: [{ ...two.options[0], total: 1 / 3 }, two.options[1]] };
	const g = t.floor.sides_fields(one_dish(shared), () => {}).filter((x) => /^s_0_/.test(x.fieldname || ""));
	assert.deepEqual(JSON.parse(JSON.stringify(g.map((x) => [x.fieldtype, x.precision]))), [["Float", "3"], ["Float", "3"]]);
	// Several dishes together: a total, never a choice of one.
	const many = t.floor.sides_fields({ per_dish: 0, slots: [{ ...RICE, max: 2 }] }, () => {});
	assert.ok(!many.some((x) => x.fieldname === "r_0"));
	t.done();
});
