frappe.ui.form.on("Resto Recipe", {
	setup(frm) {
		frm.set_query("item", () => {
			const filters = { disabled: 0, has_variants: 0, is_fixed_asset: 0 };
			filters.is_stock_item = frm.doc.recipe_type === "Menu Dish" ? 0 : 1;
			return { filters };
		});

		const row_query = () => {
			const filters = { disabled: 0, has_variants: 0, is_fixed_asset: 0 };
			if (frm.doc.recipe_type !== "Menu Dish") filters.is_stock_item = 1;
			return { filters };
		};
		frm.set_query("item_code", "items", row_query);
		frm.set_query("item_code", "sides", row_query);
	},

	refresh(frm) {
		frm.trigger("set_labels");

		if (!frm.is_new()) {
			frm.add_custom_button(__("Update Cost"), () =>
				frm.call("update_cost").then(() => frm.reload_doc())
			);
		}
	},

	recipe_type(frm) {
		frm.trigger("set_labels");
		if (frm.doc.recipe_type === "Menu Dish") {
			frm.set_value("quantity", 1);
		} else {
			frm.clear_table("sides");
			frm.refresh_field("sides");
		}
	},

	set_labels(frm) {
		const is_menu = frm.doc.recipe_type === "Menu Dish";
		frm.set_df_property("items", "label", is_menu ? __("Main Ingredients") : __("Ingredients"));
	},
});
