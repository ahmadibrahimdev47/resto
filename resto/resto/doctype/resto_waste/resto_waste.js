// A new waste starts with the kitchen warehouse and the food waste account of Resto Settings
// (D-239); the lists offer only the company's non-group warehouses and accounts.
frappe.ui.form.on("Resto Waste", {
	setup(frm) {
		frm.set_query("warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("waste_account", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
	},
	onload(frm) {
		if (!frm.is_new()) return;
		frappe.xcall("resto.resto.doctype.resto_waste.resto_waste.new_waste_defaults").then((d) => {
			for (const field of ["company", "warehouse", "waste_account"]) {
				if (d[field] && !frm.doc[field]) frm.set_value(field, d[field]);
			}
		});
	},
});
