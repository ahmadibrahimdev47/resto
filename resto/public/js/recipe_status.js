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
				const pct = r.food_cost_percent == null ? "" : `${flt(r.food_cost_percent, 1)}%`;
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

	d.fields_dict.list.$wrapper.html(`<p class="text-muted">${__("Loading...")}</p>`);
	d.show();
	frappe.call({
		method: "resto.resto.doctype.resto_recipe.resto_recipe.recipe_status",
		callback: (r) => {
			data = r.message || data;
			// Missing first: that is the work still to do.
			if (data.missing) d.set_value("show", "Missing");
			else draw();
		},
	});
};
