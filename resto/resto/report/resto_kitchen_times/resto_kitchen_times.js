frappe.query_reports["Resto Kitchen Times"] = {
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
			options: [{ value: "Station", label: __("Station") }, { value: "Item", label: __("Item") }, { value: "Day", label: __("Day") }, { value: "Hour", label: __("Hour") }, { value: "Waiter", label: __("Waiter") }],
			default: "Station",
		},
		{
			fieldname: "station",
			label: __("Station"),
			fieldtype: "Link",
			options: "Resto Kitchen Station",
		},
		{
			fieldname: "late_minutes",
			label: __("Late After (Minutes)"),
			fieldtype: "Int",
			default: 20,
		},
	],
};
