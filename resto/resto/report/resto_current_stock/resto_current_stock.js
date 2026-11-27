// What each store holds now, and what came in and went out over the dates: ERPNext's Stock Balance
// in the columns a storekeeper reads (D-285), and what is short of its reorder level (D-287).
frappe.query_reports["Resto Current Stock"] = {
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
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: () => ({ filters: { company: frappe.query_report.get_filter_value("company") } }),
		},
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group",
		},
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "short_only",
			label: __("Short Items Only"),
			fieldtype: "Check",
		},
	],
	// At or below its reorder level (D-287): the item and its balance in red, the short rows first.
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (data && data.short && ["item_code", "bal_qty", "to_order"].includes(column.fieldname)) {
			value = `<span style="color:var(--red-600, #e03131);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
