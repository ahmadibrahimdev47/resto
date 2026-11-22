// Recipe tab on Item. The Resto Recipe stays the only stored copy: the server fills this tab on
// load and writes it back to the recipe when the item is saved (resto/resto/item_recipe.py).
(() => {
	const P = "resto_recipe_";
	const TABLES = [P + "items", P + "sides"];
	const COSTS = { total_cost: P + "total_cost", cost_per_unit: P + "cost_per_unit", selling_rate: P + "selling_rate", food_cost_percent: P + "food_cost_percent" };
	const PREPARED = "Prepared Ingredient";
	const MENU = "Menu Dish";

	const rows = (frm) => TABLES.flatMap((t) => frm.doc[t] || []);

	function mark_changed(frm) {
		if (frm.doc[P + "changed"]) return;
		frm.doc[P + "changed"] = 1;
		frm.dirty();
	}

	function preview(frm) {
		clearTimeout(frm._resto_item_cost_timer);
		frm._resto_item_cost_timer = setTimeout(() => {
			if (!frm.doc[P + "type"] || !rows(frm).some((r) => r.item_code)) return;
			const seq = (frm._resto_item_cost_seq = (frm._resto_item_cost_seq || 0) + 1);
			const strip = (r) => ({ name: r.name, item_code: r.item_code, qty: r.qty, uom: r.uom });
			const doc = {
				doctype: "Resto Recipe",
				recipe_type: frm.doc[P + "type"],
				item: frm.doc.item_code,
				quantity: frm.doc[P + "quantity"] || 1,
				items: (frm.doc[P + "items"] || []).map(strip),
				sides: frm.doc[P + "type"] === MENU ? (frm.doc[P + "sides"] || []).map(strip) : [],
			};
			frappe.call({
				method: "resto.resto.doctype.resto_recipe.resto_recipe.preview_cost",
				args: { doc },
				type: "POST",
				callback: (r) => {
					if (!r.message || seq !== frm._resto_item_cost_seq || frm.doctype !== "Item") return;
					const by_name = {};
					for (const row of rows(frm)) by_name[row.name] = row;
					for (const c of r.message.rows) {
						const row = by_name[c.name];
						if (row) Object.assign(row, { rate: c.rate, amount: c.amount, stock_qty: c.stock_qty, conversion_factor: c.conversion_factor });
					}
					for (const [src, field] of Object.entries(COSTS)) frm.doc[field] = r.message[src];
					TABLES.forEach((t) => frm.refresh_field(t));
					frm.refresh_fields(Object.values(COSTS));
					window.resto?.show_food_cost?.(frm, P + "food_cost_percent", P + "target_food_cost_percent");
				},
			});
		}, 400);
	}

	function render(frm) {
		window.resto?.show_food_cost?.(frm, P + "food_cost_percent", P + "target_food_cost_percent");
		const note = frm.get_field(P + "note");
		const link = frm.get_field(P + "link");
		if (note) {
			note.$wrapper.html(
				frm.doc.has_variants
					? `<div class="alert alert-info">${__("This item is a template. Make a recipe for each size (variant) instead.")}</div>`
					: `<p class="text-muted">${__("Prepared ingredient for a stock item made in the kitchen, menu dish for a non-stock item sold at the POS. Saving the item saves its recipe.")}</p>`
			);
		}
		if (link) {
			const has = !frm.is_new() && frm.doc[P + "modified"];
			link.$wrapper.html(
				has
					? `<a class="btn btn-default btn-xs" href="/app/resto-recipe/${encodeURIComponent(frm.doc.name)}">${__("Open the Recipe")}</a>`
					: ""
			);
		}
	}

	frappe.ui.form.on("Item", {
		setup(frm) {
			const query = () => {
				const filters = { disabled: 0, has_variants: 0, is_fixed_asset: 0 };
				if (frm.doc[P + "type"] !== MENU) filters.is_stock_item = 1;
				return { filters };
			};
			TABLES.forEach((t) => frm.set_query("item_code", t, query));
		},
		refresh: render,
		has_variants: render,
		resto_needs_recipe(frm) {
			// Ticking it opens the recipe part of the tab; start with the type that fits the item.
			if (frm.doc.resto_needs_recipe && !frm.doc[P + "type"] && !frm.doc.has_variants) {
				frm.set_value(P + "type", frm.doc.is_stock_item ? PREPARED : MENU);
			}
		},
		[P + "type"](frm) {
			mark_changed(frm);
			if (frm.doc[P + "type"] !== MENU && (frm.doc[P + "sides"] || []).length) {
				frm.clear_table(P + "sides");
				frm.refresh_field(P + "sides");
			}
			preview(frm);
		},
		[P + "quantity"](frm) {
			mark_changed(frm);
			preview(frm);
		},
		[P + "par_qty"]: mark_changed,
		[P + "target_food_cost_percent"](frm) {
			mark_changed(frm);
			window.resto?.show_food_cost?.(frm, P + "food_cost_percent", P + "target_food_cost_percent");
		},
		[P + "preparation"](frm) {
			// The editor fires on load too; only a real edit counts.
			if (frm._resto_loaded) mark_changed(frm);
		},
		onload_post_render(frm) {
			setTimeout(() => (frm._resto_loaded = true), 500);
		},
	});

	const on_row = (frm) => {
		if (frm.doctype !== "Item") return;
		mark_changed(frm);
		if (!frm.doc[P + "type"] && !frm.doc.has_variants) {
			frm.set_value(P + "type", frm.doc.is_stock_item ? PREPARED : MENU);
		}
		preview(frm);
	};
	frappe.ui.form.on("Resto Recipe Item", {
		item_code: on_row,
		qty: on_row,
		uom: on_row,
		note(frm) {
			if (frm.doctype === "Item") mark_changed(frm);
		},
		[P + "items_remove"]: on_row,
		[P + "sides_remove"]: on_row,
	});
})();
