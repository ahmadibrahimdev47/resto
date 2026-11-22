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
				const pct = r.food_cost_percent == null ? "" : resto.food_cost_html(r.food_cost_percent, r.target_food_cost_percent);
				// The dish's own target in bold; the Resto Settings one muted.
				const target =
					r.food_cost_percent == null
						? ""
						: flt(r.target_food_cost_percent)
						? `<b>${format_number(r.target_food_cost_percent, null, 1)}%</b>`
						: `<span class="text-muted">${format_number(resto.food_cost_target(), null, 1)}%</span>`;
				return `<tr class="resto-rs-row" data-i="${i}" style="cursor:pointer">
					<td><span class="indicator-pill ${color}">${label}</span></td>
					<td><b>${esc(r.item_name)}</b><br><small class="text-muted">${esc(r.item)} · ${esc(r.item_group)}</small></td>
					<td>${esc(__(r.recipe_type || r.expected_type))}</td>
					<td class="text-end">${cost}</td>
					<td class="text-end">${pct}</td>
					<td class="text-end">${target}</td>
				</tr>`;
			})
			.join("");
		d.fields_dict.list.$wrapper.html(
			head +
				`<div style="max-height:60vh;overflow:auto"><table class="table table-hover table-sm">
				<thead><tr><th>${__("Status")}</th><th>${__("Item")}</th><th>${__("Recipe Type")}</th>
				<th class="text-end">${__("Cost")}</th><th class="text-end">${__("Food Cost %")}</th>
				<th class="text-end">${__("Target Food Cost %")}</th></tr></thead>
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


// Food cost % levels (resto/resto/food_cost.py): green up to the target, yellow and orange up to
// the target plus the Resto Settings steps, red above; 100% or more is a full red bar marked as
// over. The target is the recipe's own, or the Resto Settings one (sent through boot).
resto.food_cost_settings = () => (frappe.boot.resto || {}).food_cost || { target: 35, yellow: 15, orange: 30 };

resto.food_cost_target = (target) => flt(target) || flt(resto.food_cost_settings().target);

resto.food_cost_level = function (value, target) {
	const s = resto.food_cost_settings();
	const t = resto.food_cost_target(target);
	const levels = { green: t, yellow: t + flt(s.yellow), orange: t + flt(s.orange) };
	const v = flt(value);
	if (v >= 100) return { color: "red", over: true, target: t };
	for (const color of ["green", "yellow", "orange"]) {
		if (v <= levels[color]) return { color, target: t };
	}
	return { color: "red", target: t };
};

resto.food_cost_html = function (value, target) {
	if (value == null || value === "") return "";
	const v = flt(value);
	const level = resto.food_cost_level(v, target);
	const css = { green: "var(--green-500)", yellow: "var(--yellow-500)", orange: "var(--orange-500)", red: "var(--red-500)" }[level.color];
	const text = `${format_number(v, null, 1)}%${level.over ? " ⚠" : ""}`;
	const title = [
		level.over ? __("The recipe costs more than the selling price") : "",
		__("Target: {0}%", [format_number(level.target, null, 1)]),
	].filter(Boolean).join(" · ");
	return `<span style="display:inline-flex;align-items:center;gap:6px;width:100%" title="${frappe.utils.escape_html(title)}">
		<span style="flex:1;min-width:40px;height:6px;border-radius:3px;background:var(--gray-200);overflow:hidden">
			<span style="display:block;height:100%;width:${Math.min(v, 100)}%;background:${css}"></span>
		</span>
		<span style="color:${css};font-weight:${level.color === "green" ? 400 : 600};white-space:nowrap">${text}</span>
	</span>`;
};

// Under the food cost % field of a form (recipe form, Item Recipe tab): the level bar, and on
// the target field which target is in use.
resto.show_food_cost = function (frm, fieldname, target_field) {
	const v = frm.doc[fieldname];
	const target = frm.doc[target_field];
	frm.set_df_property(fieldname, "description", v ? resto.food_cost_html(v, target) : "");
	frm.set_df_property(
		target_field,
		"description",
		flt(target)
			? __("This dish's own target. Empty: the target in Resto Settings is used ({0}%).", [format_number(resto.food_cost_settings().target, null, 1)])
			: __("Empty: the target in Resto Settings is used ({0}%).", [format_number(resto.food_cost_settings().target, null, 1)])
	);
};

// A line at the top of the recipe form when a dish is above its target.
resto.food_cost_headline = function (frm) {
	const v = flt(frm.doc.food_cost_percent);
	const level = resto.food_cost_level(v, frm.doc.target_food_cost_percent);
	if (frm.doc.recipe_type !== "Menu Dish" || !frm.doc.selling_rate || level.color === "green") {
		frm.set_intro("");
		return;
	}
	const n = format_number(v, null, 1);
	frm.set_intro(
		level.over
			? `⚠ ${__("The dish costs more than its selling price ({0}%).", [n])}`
			: __("Food cost is {0}% of the selling price, above the {1}% target.", [n, format_number(level.target, null, 1)]),
		level.color
	);
};
