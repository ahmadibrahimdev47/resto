// The month's salary entry (D-249): the workers come with their monthly salary and open advance;
// what will be posted is shown before it is submitted.
const SALARY_PRECISION = 2;
const round2 = (v) => flt(v, SALARY_PRECISION);

frappe.ui.form.on("Resto Salary Entry", {
	setup(frm) {
		frm.set_query("pay_from_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, account_type: ["in", ["Cash", "Bank"]] },
		}));
	},
	onload(frm) {
		if (!frm.is_new()) return;
		frappe.xcall("resto.resto.doctype.resto_salary_entry.resto_salary_entry.new_entry_defaults").then((d) => {
			for (const field of ["company", "pay_from_account", "salary_account", "advances_account"]) {
				if (d[field] && !frm.doc[field]) frm.set_value(field, d[field]);
			}
			if (!(frm.doc.workers || []).length && frm.doc.company) get_workers(frm);
		});
	},
	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Get Workers"), () => get_workers(frm));
		}
		show_preview(frm);
	},
	company(frm) {
		if (frm.doc.docstatus === 0 && frm.doc.company) get_workers(frm);
	},
});

frappe.ui.form.on("Resto Salary Entry Worker", {
	salary: (frm, cdt, cdn) => row_changed(frm, cdt, cdn),
	deduct_amount: (frm, cdt, cdn) => row_changed(frm, cdt, cdn),
	deduct(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		// Ticked again: the open advance, up to the salary; unticked: it stays owed.
		frappe.model.set_value(cdt, cdn, "deduct_amount", row.deduct ? Math.min(flt(row.owed), flt(row.salary)) : 0);
	},
	workers_remove: (frm) => totals(frm),
});

function get_workers(frm) {
	return frappe
		.xcall("resto.resto.doctype.resto_salary_entry.resto_salary_entry.get_workers", { company: frm.doc.company })
		.then((rows) => {
			frm.clear_table("workers");
			for (const r of rows) frm.add_child("workers", r);
			frm.refresh_field("workers");
			totals(frm);
			if (!rows.length) {
				frappe.msgprint(__("No active worker has a Monthly Salary yet: set it on each worker's record (Workers)"));
			}
		});
}

function row_changed(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "net", round2(flt(row.salary) - (row.deduct ? flt(row.deduct_amount) : 0)));
	totals(frm);
}

function totals(frm) {
	let salaries = 0;
	let deducted = 0;
	for (const r of frm.doc.workers || []) {
		salaries += flt(r.salary);
		deducted += r.deduct ? flt(r.deduct_amount) : 0;
	}
	frm.set_value("total_salaries", round2(salaries));
	frm.set_value("total_deducted", round2(deducted));
	frm.set_value("total_net", round2(salaries - deducted));
	show_preview(frm);
}

// The lines of the Journal Entry the submit makes, before it is made.
function show_preview(frm) {
	if (frm.doc.docstatus !== 0 || !flt(frm.doc.total_salaries)) {
		frm.set_intro("");
		return;
	}
	const money = (v) => format_currency(v, frappe.defaults.get_default("currency"));
	const esc = frappe.utils.escape_html;
	const taken = (frm.doc.workers || []).filter((r) => r.deduct && flt(r.deduct_amount));
	const lines = [
		`<li>${__("Debit {0}: {1}", [esc(frm.doc.salary_account || __("Salaries Account")), money(frm.doc.total_salaries)])}</li>`,
		...taken.map(
			(r) => `<li>${__("Credit {0} for {1}: {2}", [esc(frm.doc.advances_account || ""), esc(r.employee_name || r.employee), money(r.deduct_amount)])}</li>`
		),
		flt(frm.doc.total_net) ? `<li>${__("Credit {0}: {1}", [esc(frm.doc.pay_from_account || ""), money(frm.doc.total_net)])}</li>` : "",
	];
	frm.set_intro(`<b>${__("Submitting posts one Journal Entry:")}</b><ul>${lines.join("")}</ul>`, "blue");
}
