// Daily prep: fill rows from par levels and draft the raw material transfer to the kitchen.
frappe.ui.form.on("Resto Prep", {
	refresh(frm) {
		if (frm.doc.docstatus !== 0) return;
		frm.add_custom_button(__("Get Suggestions"), () => {
			const run = () =>
				frm.call({ method: "get_suggestions", doc: frm.doc, freeze: true }).then(() => {
					frm.refresh_fields();
					frm.dirty();
				});
			if ((frm.doc.items || []).length) {
				frappe.confirm(__("Replace the current rows with the suggested quantities?"), run);
			} else {
				run();
			}
		});
		frm.add_custom_button(__("Transfer Raw Materials"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the daily prep first"));
				return;
			}
			frm.call({ method: "make_raw_material_transfer", doc: frm.doc, freeze: true }).then((r) => {
				if (r.message) frappe.set_route("Form", "Stock Entry", r.message);
			});
		});
	},
});
