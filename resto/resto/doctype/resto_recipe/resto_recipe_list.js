// The helpers come from resto.bundle.js. If a browser still runs an older copy of it, a missing
// helper is skipped (plain number, no button) instead of leaving the list blank.
frappe.listview_settings["Resto Recipe"] = {
	add_fields: ["recipe_type", "target_food_cost_percent"],
	formatters: {
		// Level colors and the number next to the bar (see resto.food_cost_html).
		food_cost_percent(value, df, doc) {
			if (doc.recipe_type !== "Menu Dish") return "";
			if (window.resto?.food_cost_html) return resto.food_cost_html(value, doc.target_food_cost_percent);
			return value == null || value === "" ? "" : `${format_number(value, null, 1)}%`;
		},
	},
	onload(listview) {
		if (window.resto?.recipe_status) {
			listview.page.add_inner_button(__("Recipe Status"), () => resto.recipe_status());
		}
		if (window.resto?.can_update_costs?.() && resto.update_all_recipe_costs) {
			listview.page.add_inner_button(__("Update All Recipe Costs"), () =>
				resto.update_all_recipe_costs(() => listview.refresh())
			);
		}
	},
};
