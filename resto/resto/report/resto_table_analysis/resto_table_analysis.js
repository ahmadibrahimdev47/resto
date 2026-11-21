frappe.query_reports["Resto Table Analysis"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default('Company'),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
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
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: [{ value: "Table", label: __("Table", null, "Resto") }, { value: "Hall", label: __("Hall") }, { value: "Waiter", label: __("Waiter") }, { value: "Weekday", label: __("Weekday") }, { value: "Hour", label: __("Hour") }],
			default: "Table",
		},
		{
			fieldname: "open_hours",
			label: __("Opening Hours per Day"),
			fieldtype: "Float",
			default: 12,
		},
	],
};
