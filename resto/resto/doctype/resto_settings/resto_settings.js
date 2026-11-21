frappe.ui.form.on("Resto Settings", {
	setup(frm) {
		const by_company = () => ({ filters: { company: frm.doc.company, is_group: 0 } });
		frm.set_query("main_warehouse", by_company);
		frm.set_query("kitchen_warehouse", by_company);
		frm.set_query("waste_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
		frm.set_query("selling_price_list", () => ({ filters: { selling: 1, enabled: 1 } }));
	},

	refresh(frm) {
		frm.add_custom_button(__("Setup Check"), () =>
			frappe.call({
				method: "resto.resto.setup_check.run",
				freeze: true,
				callback: (r) => show_setup_check(r.message),
			})
		);

		frm.add_custom_button(__("Import Existing BOMs and Bundles"), () => {
			frappe.confirm(
				__("Create a recipe for every default BOM and every Product Bundle that has no recipe yet?"),
				() =>
					frappe.call({
						method: "resto.api.import_existing_recipes",
						freeze: true,
						freeze_message: __("Importing..."),
						callback: (r) => show_import_result(r.message),
					})
			);
		});

		frm.add_custom_button(__("Update All Recipe Costs"), () =>
			frappe.call({
				method: "resto.api.update_all_recipe_costs",
				freeze: true,
				callback: (r) =>
					frappe.show_alert({
						message: __("{0} recipes updated", [r.message]),
						indicator: "green",
					}),
			})
		);
	},
});

function show_setup_check(rows) {
	const icon = { error: "red", warning: "orange", ok: "green" };
	const label = { error: __("Error"), warning: __("Warning"), ok: __("OK") };
	const errors = rows.filter((r) => r.level === "error").length;
	const warnings = rows.filter((r) => r.level === "warning").length;
	const body = rows
		.map(
			(r) => `<tr><td><span class="indicator-pill ${icon[r.level]}">${label[r.level]}</span></td>
			<td>${frappe.utils.escape_html(r.area)}</td><td>${frappe.utils.escape_html(r.message)}</td></tr>`
		)
		.join("");
	frappe.msgprint({
		title: __("Setup Check"),
		indicator: errors ? "red" : warnings ? "orange" : "green",
		message: `<p>${__("Errors")}: <b>${errors}</b> &nbsp; ${__("Warnings")}: <b>${warnings}</b></p>
			<table class="table table-bordered table-sm"><tbody>${body}</tbody></table>`,
		wide: true,
	});
}

function show_import_result(res) {
	const list = (rows) => rows.map((r) => `<li>${frappe.utils.escape_html(r)}</li>`).join("");
	const created = __("Created", null, "Resto Import");
	const skipped = __("Skipped", null, "Resto Import");
	const failed = __("Failed", null, "Resto Import");
	let html = `<p>${created}: <b>${res.created.length}</b> &nbsp; ${skipped}: <b>${res.skipped.length}</b> &nbsp; ${failed}: <b>${res.failed.length}</b></p>`;
	if (res.failed.length) {
		html += `<p><b>${failed}</b></p><ul>${list(res.failed)}</ul>`;
	}
	frappe.msgprint({ title: __("Import Result"), message: html, wide: true });
}
