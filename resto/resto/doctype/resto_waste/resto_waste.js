// A new waste starts with the kitchen warehouse and the food waste account of Resto Settings
// (D-239). Who cannot pick a warehouse or an account (the kitchen) leaves both to the server,
// which fills them on save (D-241): the form would check each link and refuse it.
const can_link = (dt) => frappe.model.can_select(dt) || frappe.model.can_read(dt);
const can_pick = () => can_link("Warehouse") && can_link("Account");

frappe.ui.form.on("Resto Waste", {
	setup(frm) {
		frm.set_query("warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("waste_account", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
	},
	onload(frm) {
		if (!frm.is_new()) return;
		const fields = can_pick() ? ["company", "warehouse", "waste_account"] : ["company"];
		frappe.xcall("resto.resto.doctype.resto_waste.resto_waste.new_waste_defaults").then((d) => {
			for (const field of fields) {
				if (d[field] && !frm.doc[field]) frm.set_value(field, d[field]);
			}
		});
	},
	refresh(frm) {
		const locked = !can_pick();
		for (const field of ["warehouse", "waste_account"]) {
			frm.set_df_property(field, "read_only", locked || frm.doc.docstatus ? 1 : 0);
		}
	},
});

// A row's quantity is typed in a recipe unit (grams for an item stocked by the kilo, millilitres by
// the litre, D-250) or another unit the item converts to; the stock quantity next to it follows.
const waste_units = {};
const units_of = (item_code) =>
	(waste_units[item_code] ||= frappe
		.xcall("resto.resto.units.item_units", { item_code })
		.catch((e) => {
			delete waste_units[item_code];
			throw e;
		}));

async function waste_stock_qty(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row || !row.item_code) return;
	const r = await units_of(row.item_code);
	const unit = r.units.find((u) => u.uom === row.entered_uom);
	if (unit) frappe.model.set_value(cdt, cdn, "qty", flt(flt(row.entered_qty) * flt(unit.factor), r.precision));
}

frappe.ui.form.on("Resto Waste", {
	setup(frm) {
		// Only the units the row's item truly converts to, from the server (no unit before the item).
		frm.set_query("entered_uom", "items", (doc, cdt, cdn) => ({
			query: "resto.resto.units.uom_query",
			filters: { item_code: (locals[cdt][cdn] || {}).item_code || "" },
		}));
	},
});

frappe.ui.form.on("Resto Waste Item", {
	async item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		const r = await units_of(row.item_code);
		const now = locals[cdt][cdn];
		if (!now || now.item_code !== row.item_code) return;
		if (!r.units.some((u) => u.uom === now.entered_uom)) await frappe.model.set_value(cdt, cdn, "entered_uom", r.default);
		waste_stock_qty(frm, cdt, cdn);
	},
	entered_qty: (frm, cdt, cdn) => waste_stock_qty(frm, cdt, cdn),
	entered_uom: (frm, cdt, cdn) => waste_stock_qty(frm, cdt, cdn),
});
