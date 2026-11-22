frappe.listview_settings["Resto Recipe"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Recipe Status"), () => resto.recipe_status());
		if (resto.can_update_costs()) {
			listview.page.add_inner_button(__("Update All Recipe Costs"), () =>
				resto.update_all_recipe_costs(() => listview.refresh())
			);
		}
	},
};
