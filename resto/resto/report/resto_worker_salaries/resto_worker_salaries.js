// What each worker was paid, month by month, from the submitted salary entries (D-261).
frappe.query_reports["Resto Worker Salaries"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", default: frappe.defaults.get_user_default("Company"), reqd: 1 },
		{ fieldname: "from_month", label: __("From Month"), fieldtype: "Date", default: frappe.datetime.add_months(frappe.datetime.month_start(), -11) },
		{ fieldname: "to_month", label: __("To Month"), fieldtype: "Date", default: frappe.datetime.month_start() },
		{ fieldname: "employee", label: __("Employee"), fieldtype: "Link", options: "Employee" },
		{ fieldname: "by_worker", label: __("Total per Worker"), fieldtype: "Check", default: 0 },
	],
};
