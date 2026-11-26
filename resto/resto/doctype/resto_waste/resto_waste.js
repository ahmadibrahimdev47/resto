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
