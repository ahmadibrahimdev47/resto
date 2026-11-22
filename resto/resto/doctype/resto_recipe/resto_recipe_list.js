frappe.listview_settings["Resto Recipe"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Recipe Status"), () => resto.recipe_status());
	},
};
