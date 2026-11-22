// Recipe Status: every item ticked "Needs a Recipe" with its recipe state. Opened from the
// Resto Recipe list and form. A row opens the recipe, or a new one for that item.
frappe.provide("resto");

resto.recipe_status = function () {
	const STATUS = {
		Active: ["green", __("Active")],
		Inactive: ["orange", __("Inactive")],
		Missing: ["red", __("Missing", null, "Resto")],
	};
	const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
	const d = new frappe.ui.Dialog({
		title: __("Recipe Status"),
		size: "extra-large",
		fields: [
			{
				fieldname: "show",
				fieldtype: "Select",
				label: __("Show", null, "Resto"),
				options: [
					{ value: "", label: __("All") },
					{ value: "Missing", label: __("Missing", null, "Resto") },
					{ value: "Inactive", label: __("Inactive") },
					{ value: "Active", label: __("Active") },
				],
				default: "",
				change: () => draw(),
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "search", fieldtype: "Data", label: __("Search"), change: () => draw() },
			{ fieldtype: "Section Break" },
			{ fieldname: "list", fieldtype: "HTML" },
		],
	});
	let data = { rows: [], missing: 0 };

	function draw() {
		const show = d.get_value("show");
		const q = (d.get_value("search") || "").trim().toLowerCase();
		const rows = data.rows.filter(
			(r) => (!show || r.status === show) && (!q || `${r.item} ${r.item_name} ${r.item_group}`.toLowerCase().includes(q))
		);
		const head = `<p>${__("{0} items need a recipe; {1} have none yet.", [data.rows.length, `<b class="text-danger">${data.missing}</b>`])}</p>`;
		if (!rows.length) {
			d.fields_dict.list.$wrapper.html(head + `<p class="text-muted">${__("Nothing to show. Tick Needs a Recipe on items to list them here.")}</p>`);
			return;
		}
		const body = rows
			.map((r, i) => {
				const [color, label] = STATUS[r.status];
				const cost = r.cost == null ? "" : format_currency(r.cost) + (r.recipe_type === "Prepared Ingredient" ? ` / ${esc(__(r.uom))}` : "");
				const pct = r.food_cost_percent == null ? "" : resto.food_cost_html(r.food_cost_percent);
				return `<tr class="resto-rs-row" data-i="${i}" style="cursor:pointer">
					<td><span class="indicator-pill ${color}">${label}</span></td>
					<td><b>${esc(r.item_name)}</b><br><small class="text-muted">${esc(r.item)} · ${esc(r.item_group)}</small></td>
					<td>${esc(__(r.recipe_type || r.expected_type))}</td>
					<td class="text-end">${cost}</td>
					<td class="text-end">${pct}</td>
				</tr>`;
			})
			.join("");
		d.fields_dict.list.$wrapper.html(
			head +
				`<div style="max-height:60vh;overflow:auto"><table class="table table-hover table-sm">
				<thead><tr><th>${__("Status")}</th><th>${__("Item")}</th><th>${__("Recipe Type")}</th>
				<th class="text-end">${__("Cost")}</th><th class="text-end">${__("Food Cost %")}</th></tr></thead>
				<tbody>${body}</tbody></table></div>`
		);
		d.fields_dict.list.$wrapper.find(".resto-rs-row").on("click", (e) => {
			const r = rows[cint($(e.currentTarget).attr("data-i"))];
			d.hide();
			if (r.recipe) frappe.set_route("Form", "Resto Recipe", r.recipe);
			else frappe.new_doc("Resto Recipe", { item: r.item, recipe_type: r.expected_type });
		});
	}

	function load(first) {
		d.fields_dict.list.$wrapper.html(`<p class="text-muted">${__("Loading...")}</p>`);
		frappe.call({
			method: "resto.resto.doctype.resto_recipe.resto_recipe.recipe_status",
			callback: (r) => {
				data = r.message || data;
				// Missing first: that is the work still to do.
				if (first && data.missing) d.set_value("show", "Missing");
				else draw();
			},
		});
	}

	if (resto.can_update_costs()) {
		d.set_primary_action(__("Update All Recipe Costs"), () => resto.update_all_recipe_costs(() => load(false)));
	}
	d.show();
	load(true);
};

resto.can_update_costs = () =>
	frappe.user.has_role(["System Manager", "Resto Manager", "Manufacturing Manager", "Stock Manager"]);

// Same method as the Resto Settings button and the nightly job.
resto.update_all_recipe_costs = function (after) {
	frappe.call({
		method: "resto.api.update_all_recipe_costs",
		freeze: true,
		freeze_message: __("Updating recipe costs..."),
		callback: (r) => {
			frappe.show_alert({ message: __("{0} recipes updated", [r.message]), indicator: "green" });
			if (after) after();
		},
	});
};

// Food cost % with levels: green up to the alert level (Resto Settings), orange up to 10 points
// above it, red beyond; 100% or more is a full red bar marked as over.
resto.food_cost_level = function (value) {
	const alert = flt((frappe.boot.resto || {}).food_cost_alert) || 35;
	const v = flt(value);
	if (v >= 100) return { color: "red", over: true };
	if (v > alert + 10) return { color: "red" };
	if (v > alert) return { color: "orange" };
	return { color: "green" };
};

resto.food_cost_html = function (value) {
	if (value == null || value === "") return "";
	const v = flt(value);
	const level = resto.food_cost_level(v);
	const css = { green: "var(--green-500)", orange: "var(--orange-500)", red: "var(--red-500)" }[level.color];
	const text = `${format_number(v, null, 1)}%${level.over ? " ⚠" : ""}`;
	const title = level.over ? __("The recipe costs more than the selling price") : "";
	return `<span style="display:inline-flex;align-items:center;gap:6px;width:100%" title="${frappe.utils.escape_html(title)}">
		<span style="flex:1;min-width:40px;height:6px;border-radius:3px;background:var(--gray-200);overflow:hidden">
			<span style="display:block;height:100%;width:${Math.min(v, 100)}%;background:${css}"></span>
		</span>
		<span style="color:${css};font-weight:${level.color === "green" ? 400 : 600};white-space:nowrap">${text}</span>
	</span>`;
};

// Show the level bar under a food cost % field of a form (recipe form, Item Recipe tab).
resto.show_food_cost = function (frm, fieldname) {
	const v = frm.doc[fieldname];
	frm.set_df_property(fieldname, "description", v ? resto.food_cost_html(v) : "");
};
