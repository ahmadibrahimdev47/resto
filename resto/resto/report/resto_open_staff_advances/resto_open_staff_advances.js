// What each worker still owes of the advances paid from the drawer (D-237): taken off the salary
// at the month's end.
frappe.query_reports["Resto Open Staff Advances"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "to_date", label: __("As On"), fieldtype: "Date", default: frappe.datetime.get_today() },
		{ fieldname: "show_settled", label: __("Show Settled Workers"), fieldtype: "Check", default: 0 },
	],
};
