frappe.query_reports["Resto Sales Analysis"] = {
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
			options: [{ value: "Day", label: __("Day") }, { value: "Hour", label: __("Hour") }, { value: "Weekday", label: __("Weekday") }, { value: "Month", label: __("Month") }, { value: "Order Type", label: __("Order Type") }, { value: "Table", label: __("Table") }, { value: "Hall", label: __("Hall") }, { value: "Waiter", label: __("Waiter") }, { value: "Cashier", label: __("Cashier") }, { value: "POS Profile", label: __("POS Profile") }, { value: "Item", label: __("Item") }, { value: "Item Group", label: __("Item Group") }, { value: "Payment Method", label: __("Payment Method") }],
			default: "Day",
		},
	],
};
