frappe.listview_settings["Resto Recipe"] = {
	add_fields: ["recipe_type", "target_food_cost_percent"],
	formatters: {
		// Level colors and the number next to the bar (see resto.food_cost_html).
		food_cost_percent(value, df, doc) {
			return doc.recipe_type === "Menu Dish" ? resto.food_cost_html(value, doc.target_food_cost_percent) : "";
		},
	},
	onload(listview) {
		listview.page.add_inner_button(__("Recipe Status"), () => resto.recipe_status());
		if (resto.can_update_costs()) {
			listview.page.add_inner_button(__("Update All Recipe Costs"), () =>
				resto.update_all_recipe_costs(() => listview.refresh())
			);
		}
	},
};
