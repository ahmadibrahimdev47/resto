frappe.query_reports["Resto Voids and Discounts"] = {
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
			fieldname: "type",
			label: __("Show", null, "Resto"),
			fieldtype: "Select",
			options: [{ value: "Voided Items", label: __("Voided Items") }, { value: "Discounts", label: __("Discounts") }, { value: "Free Extras", label: __("Free Extras") }, { value: "Cancelled Orders", label: __("Cancelled Orders") }, { value: "Cancelled Invoices", label: __("Cancelled Invoices") }],
			default: "Voided Items",
		},
	],
};
