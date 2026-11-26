// One portion of an option is typed in a recipe unit (D-264): grams for an item stocked by the kilo,
// millilitres by the litre, or another unit the item converts to; the stock quantity that is
// deducted follows, as on a waste row (D-250).
const side_units = {};
const side_units_of = (item_code) =>
	(side_units[item_code] ||= frappe.xcall("resto.resto.units.item_units", { item_code }).catch((e) => {
		delete side_units[item_code];
		throw e;
	}));

async function side_portion_qty(cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row || !row.item) return;
	const r = await side_units_of(row.item);
	const unit = r.units.find((u) => u.uom === (row.entered_uom || r.stock_uom));
	if (unit) frappe.model.set_value(cdt, cdn, "portion_qty", flt(flt(row.entered_qty) * flt(unit.factor), 6));
}

frappe.ui.form.on("Resto Side Group", {
	setup(frm) {
		// Only the units the option's item truly converts to (no unit before the item).
		frm.set_query("entered_uom", "options", (doc, cdt, cdn) => ({
			query: "resto.resto.units.uom_query",
			filters: { item_code: (locals[cdt][cdn] || {}).item || "" },
		}));
	},
});

frappe.ui.form.on("Resto Side Option", {
	async item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item) return;
		const r = await side_units_of(row.item);
		const now = locals[cdt][cdn];
		if (!now || now.item !== row.item) return;
		if (!r.units.some((u) => u.uom === now.entered_uom)) await frappe.model.set_value(cdt, cdn, "entered_uom", r.default);
		side_portion_qty(cdt, cdn);
	},
	entered_qty: (frm, cdt, cdn) => side_portion_qty(cdt, cdn),
	entered_uom: (frm, cdt, cdn) => side_portion_qty(cdt, cdn),
});
