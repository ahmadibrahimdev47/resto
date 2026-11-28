// Percentages show at most two decimals (D-292), only on screen: the input still reads what is
// typed at the float precision, and a field with its own precision keeps it.
//   node apps/resto/resto/tests/js/test_percent_display.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function load() {
	class ControlFloat {
		constructor(df) {
			this.df = df;
		}
		get_number_format() {
			return null;
		}
		get_precision() {
			return this.df.precision || 5;
		}
		parse(value) {
			return Number(Number(value).toFixed(this.get_precision()));
		}
	}
	class ControlPercent extends ControlFloat {}
	const ctx = {
		cint: (v) => parseInt(v, 10) || 0,
		cstr: (v) => (v == null ? "" : String(v)),
		format_number: (v, fmt, precision) => Number(v).toFixed(precision),
		frappe: {
			provide(name) {
				let o = ctx;
				for (const part of name.split(".")) o = o[part] = o[part] || {};
			},
			form: { formatters: { _right: (v) => v } },
			ui: { form: { ControlPercent } },
		},
	};
	vm.createContext(ctx);
	vm.runInContext(fs.readFileSync(path.join(__dirname, "../../public/js/percent_display.js"), "utf8"), ctx);
	return { ctx, ControlPercent };
}

test("a percent without its own precision shows two decimals at most", () => {
	const { ctx } = load();
	const fmt = ctx.frappe.form.formatters.Percent;
	assert.equal(fmt(61.575731, {}), "61.58%");
	assert.equal(fmt(12.5, {}), "12.5%");
	assert.equal(fmt(14, {}), "14%");
	assert.equal(fmt(0.003791, { precision: "" }), "0.00%");
	assert.equal(fmt(null, {}), "");
});

test("a field that sets its own precision keeps it", () => {
	const { ctx } = load();
	assert.equal(ctx.frappe.form.formatters.Percent(15.015015, { precision: "4" }), "15.0150%");
});

test("the input box shows two decimals but reads what is typed in full", () => {
	const { ControlPercent } = load();
	const c = new ControlPercent({ fieldtype: "Percent" });
	assert.equal(c.format_for_input(15.015015), "15.02");
	assert.equal(c.format_for_input(10), "10");
	assert.equal(c.parse("15.015015"), 15.01502); // the float precision, not two decimals
});
