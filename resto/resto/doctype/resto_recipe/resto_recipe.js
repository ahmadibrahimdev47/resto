// Live cost while typing: the server computes it with the same rules as saving (preview_cost);
// nothing is stored until the recipe is saved.
const COST_FIELDS = ["main_cost", "total_cost", "packaging_cost", "cost_per_unit", "selling_rate", "food_cost_percent"];

function preview_cost(frm) {
	// "Resto Recipe Item" events also fire on the Item form's Recipe tab (item_recipe.js).
	if (frm.doctype !== "Resto Recipe") return;
	clearTimeout(frm._resto_cost_timer);
	frm._resto_cost_timer = setTimeout(() => {
		const rows = [...(frm.doc.items || []), ...(frm.doc.sides || []), ...(frm.doc.packaging || [])];
		if (!rows.some((r) => r.item_code)) return;
		const seq = (frm._resto_cost_seq = (frm._resto_cost_seq || 0) + 1);
		frappe.call({
			method: "resto.resto.doctype.resto_recipe.resto_recipe.preview_cost",
			args: { doc: frm.doc },
			type: "POST",
			callback: (r) => {
				// A newer request is on its way, or the user left the form.
				if (!r.message || seq !== frm._resto_cost_seq || frm.doc.doctype !== "Resto Recipe") return;
				apply_cost(frm, r.message);
			},
		});
	}, 400);
}

function apply_cost(frm, cost) {
	const by_name = {};
	for (const r of [...(frm.doc.items || []), ...(frm.doc.sides || []), ...(frm.doc.packaging || [])]) by_name[r.name] = r;
	for (const c of cost.rows) {
		const row = by_name[c.name] || (frm.doc[c.parentfield] || []).find((r) => r.idx === c.idx);
		if (!row) continue;
		// Plain assignment: cost fields are read only and must not re-trigger this preview.
		Object.assign(row, { rate: c.rate, amount: c.amount, stock_qty: c.stock_qty, conversion_factor: c.conversion_factor });
	}
	for (const f of COST_FIELDS) frm.doc[f] = cost[f];
	frm.refresh_field("items");
	frm.refresh_field("sides");
	frm.refresh_field("packaging");
	frm.refresh_fields(COST_FIELDS);
	window.resto?.show_food_cost?.(frm, "food_cost_percent", "target_food_cost_percent");
	window.resto?.food_cost_headline?.(frm);
}

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
		// Packaging is taken as its own stock line at no price on the bill (D-217, D-224).
		frm.set_query("item_code", "packaging", () => ({ filters: { disabled: 0, has_variants: 0, is_fixed_asset: 0, is_stock_item: 1, is_sales_item: 1 } }));
	},

	refresh(frm) {
		frm.trigger("set_labels");
		window.resto?.show_food_cost?.(frm, "food_cost_percent", "target_food_cost_percent");
		window.resto?.food_cost_headline?.(frm);
		frm.add_custom_button(__("Recipe Status"), () => window.resto?.recipe_status?.());

		if (!frm.is_new()) {
			frm.add_custom_button(__("Update Cost"), () =>
				frm.call("update_cost").then(() => frm.reload_doc())
			);
		}
	},

	target_food_cost_percent(frm) {
		// Only the colors depend on the target: no server call.
		window.resto?.show_food_cost?.(frm, "food_cost_percent", "target_food_cost_percent");
		window.resto?.food_cost_headline?.(frm);
	},

	recipe_type(frm) {
		frm.trigger("set_labels");
		if (frm.doc.recipe_type === "Menu Dish") {
			frm.set_value("quantity", 1);
		} else {
			frm.clear_table("sides");
			frm.refresh_field("sides");
			frm.clear_table("packaging");
			frm.refresh_field("packaging");
		}
		preview_cost(frm);
	},

	item: preview_cost,
	quantity: preview_cost,

	set_labels(frm) {
		const is_menu = frm.doc.recipe_type === "Menu Dish";
		frm.set_df_property("items", "label", is_menu ? __("Main Ingredients") : __("Ingredients"));
	},
});

frappe.ui.form.on("Resto Recipe Item", {
	item_code: preview_cost,
	qty: preview_cost,
	uom: preview_cost,
	items_remove: preview_cost,
	sides_remove: preview_cost,
	packaging_remove: preview_cost,
});
