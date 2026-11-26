// Waste for dishes the kitchen made that were voided after sending without it: an order cancelled
// without ticking them as cooked, or a void answered "not cooked" (resto.resto.floor.post_cooked_waste).
frappe.ui.form.on("Resto Order", {
	refresh(frm) {
		if (frm.doc.staff_meal && frm.doc.status === "Posted" && frappe.user.has_role(["Resto Manager", "System Manager"])) {
			frm.add_custom_button(__("Cancel Staff Meal"), () => {
				frappe.prompt(
					[{ fieldname: "reason", fieldtype: "Data", label: __("Reason"), reqd: 1 }],
					async (v) => {
						await frappe.xcall("resto.resto.staff_meal.cancel", { order: frm.doc.name, reason: v.reason });
						frappe.show_alert({ message: __("Staff meal cancelled; its stock is back"), indicator: "green" });
						frm.reload_doc();
					},
					__("Cancel Staff Meal"),
					__("Cancel Staff Meal")
				);
			});
		}
		if (frm.is_new() || !frappe.user.has_role(["Resto Cashier", "Resto Manager", "System Manager"])) return;
		const open = (frm.doc.items || []).filter((d) => d.ticket && d.kitchen_status === "Void" && !d.void_waste);
		if (!open.length) return;
		frm.add_custom_button(__("Post Waste for Cooked Dishes"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Post Waste for Cooked Dishes"),
				fields: [
					{
						fieldname: "help",
						fieldtype: "HTML",
						options: `<p class="text-muted">${__("Tick the dishes the kitchen already made: their ingredients are posted as waste.")}</p>`,
					},
					...resto.cooked_fields(open, (r) => `${frappe.utils.escape_html(r.item_name)} × ${r.qty}`, () => false),
				],
				primary_action_label: __("Post Waste"),
				primary_action: async (v) => {
					const lines = resto.cooked_values(open, v, (r) => r.name);
					if (!Object.keys(lines).length) return frappe.msgprint(__("Choose the dishes that were cooked"));
					d.hide();
					try {
						const posted = await frappe.xcall("resto.resto.floor.post_cooked_waste", { order: frm.doc.name, lines: JSON.stringify(lines) });
						// A waste that could not be posted was already shown in orange.
						if (Object.values(posted || {}).every(Boolean)) frappe.show_alert({ message: __("Waste posted"), indicator: "green" });
					} finally {
						frm.reload_doc();
					}
				},
			});
			d.show();
		});
	},
});
