import "./percent_display";

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
	// Frappe's own Percent bar (.progress / .progress-bar), in the level color.
	const width = Math.min(Math.max(v, 0), 100);
	return `<div style="display:flex;align-items:center;gap:8px;width:100%" title="${frappe.utils.escape_html(title)}">
		<div class="progress" style="margin:0;flex:1;min-width:50px">
			<div class="progress-bar" role="progressbar" aria-valuenow="${width}" aria-valuemin="0" aria-valuemax="100"
				style="width:${width}%;background-color:${css}"></div>
		</div>
		<span style="color:${css};font-weight:${level.color === "green" ? 400 : 600};white-space:nowrap">${text}</span>
	</div>`;
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

// Printing from the browser (kitchen tickets, bills, receipts): each page is one print job, printed
// from a hidden frame. Chrome started with --kiosk-printing prints it on the computer's default
// printer with no window; otherwise the browser's print window opens for each page.
// Resolves true once every page was handed to the browser's printing, false when one could not be.
resto.print_pages = async function (pages) {
	for (const html of pages || []) {
		if (!(await resto.print_html(html))) return false;
	}
	return true;
};

resto.print_html = function (html) {
	return new Promise((resolve) => {
		const frame = document.createElement("iframe");
		frame.setAttribute("sandbox", "allow-same-origin allow-modals");
		frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden;";
		let done = false;
		let started = false;
		const finish = (ok) => {
			if (done) return;
			done = true;
			// A short gap before the next job, so a slow computer does not drop it.
			setTimeout(() => {
				frame.remove();
				resolve(ok);
			}, 700);
		};
		frame.onload = () => {
			setTimeout(() => {
				try {
					const win = frame.contentWindow;
					win.addEventListener("afterprint", () => finish(true));
					win.focus();
					started = true;
					const t0 = Date.now();
					// Returns once the job is queued (kiosk printing) or the print window is closed.
					win.print();
					// Kiosk printing returns at once; a print window held it open. A few seconds may be a
					// print window closed quickly or a slow computer (print_slow: not sure it printed);
					// a long wait is a print window (print_window_seen).
					const took = Date.now() - t0;
					if (took > 3000) resto.print_slow = true;
					if (took > 8000) resto.print_window_seen = true;
					setTimeout(() => finish(true), 1500);
				} catch (e) {
					finish(false);
				}
			}, 300);
		};
		// The page never loaded: not printed.
		setTimeout(() => started || finish(false), 60000);
		frame.srcdoc = html;
		document.body.appendChild(frame);
	});
};

// Recipes are written in grams and millilitres: an ingredient stocked by the kilo or the litre gets
// that unit on its recipe row (Resto Recipe and the Item Recipe tab), unless a unit was chosen. The
// stock unit and the cost stay as they are; a row left empty still means the stock unit.
resto.RECIPE_UNITS = { Kg: "Gram", Litre: "Millilitre" };
frappe.ui.form.on("Resto Recipe Item", {
	async item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const item = row.item_code;
		if (!item || row.uom) return;
		const r = await frappe.db.get_value("Item", item, "stock_uom");
		const unit = resto.RECIPE_UNITS[r && r.message && r.message.stock_uom];
		const now = locals[cdt] && locals[cdt][cdn];
		// The user may have picked another item or a unit meanwhile.
		if (unit && now && now.item_code === item && !now.uom) frappe.model.set_value(cdt, cdn, "uom", unit);
	},
});

// The cooked dishes of a dialog: a tick for a line of one dish, how many for a line of several.
resto.cooked_fields = (rows, label, ticked) =>
	rows.map((r, i) =>
		flt(r.qty) > 1
			? { fieldname: "c_" + i, fieldtype: "Float", label: `${label(r)} — ${__("How many were cooked?")}`, default: ticked(r) ? flt(r.qty) : 0 }
			: { fieldname: "c_" + i, fieldtype: "Check", label: label(r), default: ticked(r) ? 1 : 0 }
	);
resto.cooked_values = (rows, v, key) => {
	const out = {};
	rows.forEach((r, i) => {
		const q = flt(r.qty) > 1 ? flt(v["c_" + i]) : v["c_" + i] ? flt(r.qty) : 0;
		if (q > 0) out[key(r)] = q;
	});
	return out;
};

// A floor screen locked in this session (D-216) stays locked on every desk page: a reload or the
// back button on another page, or a lock from another tab, covers the page and goes back to the floor,
// which shows the lock. A sign-out in another tab sends this one to the login page.
resto.lock_key = () => `resto_floor_locked|${frappe.session.user}`;
resto.lock_hold = function () {
	let v = null;
	try {
		v = localStorage.getItem(resto.lock_key());
	} catch (e) {
		return;
	}
	if (!v || v !== frappe.csrf_token) return;
	const route = (frappe.get_route && frappe.get_route()) || [];
	if (route[0] === "resto-floor") return; // the floor shows its lock itself (after a payment open)
	if (!$(".resto-lock-hold").length) {
		$('<div class="resto-lock-hold"></div>')
			.css({ position: "fixed", top: 0, right: 0, bottom: 0, left: 0, zIndex: 2147482999, background: "var(--bg-color, #fff)" })
			.appendTo(document.body);
	}
	frappe.set_route("resto-floor");
};
$(document).on("app_ready", () => {
	resto.lock_hold();
	if (frappe.router && frappe.router.on) frappe.router.on("change", resto.lock_hold);
	window.addEventListener("storage", (e) => {
		if (e.key !== resto.lock_key()) return;
		if (e.newValue === "out") window.location.href = "/login";
		else resto.lock_hold();
	});
});

// The red bar of a late backup or off-site copy (D-256), for managers, on the desk and the floor
// screen. Checked at load and every hour (the floor stays open all day); closed, it comes back on
// the next check while the backup is still late.
resto.backup_bar = function (alert) {
	$(".resto-backup-bar").remove();
	if (!alert || !(alert.problems || []).length) return;
	const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
	const $bar = $(
		`<div class="resto-backup-bar" role="alert"><span>${alert.problems.map(esc).join(" · ")}</span>
			<button type="button" aria-label="${__("Close")}">×</button></div>`
	).appendTo(document.body);
	$bar.find("button").on("click", () => $bar.remove());
};

resto.watch_backups = function () {
	if (resto._backup_watched || !frappe.boot || !frappe.boot.resto) return;
	resto._backup_watched = true;
	const roles = (frappe.boot.user && frappe.boot.user.roles) || [];
	if (!roles.includes("Resto Manager") && !roles.includes("System Manager")) return;
	resto.backup_bar(frappe.boot.resto.backup_alert);
	setInterval(() => {
		frappe.xcall("resto.resto.backup_watch.alert").then(resto.backup_bar).catch(() => {});
	}, 60 * 60 * 1000);
};
$(document).on("app_ready", resto.watch_backups);
$(() => setTimeout(resto.watch_backups, 2000));

// Before Submit of a posting that takes money from a cash account (D-257): when it would leave
// the account below zero, ask first (Warn Only) or stop (Block the Posting). The server checks
// again once the posting is booked.
resto.cash_guard = async function (frm) {
	let r = null;
	try {
		r = await frappe.xcall("resto.resto.cash_guard.preview", { doctype: frm.doctype, name: frm.doc.name });
	} catch (e) {
		return;
	}
	if (!r) return;
	if (r.mode === "Block the Posting") {
		frappe.validated = false;
		frappe.msgprint({ title: __("Cash Below Zero"), message: r.message, indicator: "red" });
		return;
	}
	const ok = await new Promise((resolve) =>
		frappe.confirm(`${r.message}<br><br>${__("Submit anyway?")}`, () => resolve(true), () => resolve(false))
	);
	if (!ok) frappe.validated = false;
};
for (const doctype of ["Journal Entry", "Payment Entry", "Purchase Invoice", "Resto Salary Entry"]) {
	frappe.ui.form.on(doctype, { before_submit: (frm) => resto.cash_guard(frm) });
}
