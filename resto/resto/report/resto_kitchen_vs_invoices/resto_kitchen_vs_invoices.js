// Every dish sent to the kitchen against what was billed for it; by default only what needs a look.
frappe.query_reports["Resto Kitchen vs Invoices"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			// A shift replaces the dates: from its opening to its closing (or now).
			fieldname: "shift",
			label: __("Shift"),
			fieldtype: "Link",
			options: "POS Opening Shift",
		},
		{
			fieldname: "cashier",
			label: __("Cashier"),
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "pos_profile",
			label: __("POS Profile"),
			fieldtype: "Link",
			options: "POS Profile",
		},
		{
			fieldname: "station",
			label: __("Station"),
			fieldtype: "Link",
			options: "Resto Kitchen Station",
		},
		{
			fieldname: "show",
			label: __("Show", null, "Resto"),
			fieldtype: "Select",
			options: [
				{ value: "Exceptions", label: __("Needs a Look") },
				{ value: "All Lines", label: __("All Lines") },
			],
			default: "Exceptions",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "result_label" && data && data.result_color) {
			value = `<span class="indicator-pill ${data.result_color}">${value}</span>`;
		}
		return value;
	},
};
