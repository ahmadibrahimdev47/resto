// The day's profit and loss from the general ledger (D-289): from gross sales down to net profit,
// or the cost of each item sold.
frappe.query_reports["Resto Daily Profit and Loss"] = {
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
			fieldname: "view",
			label: __("Show", null, "Resto Daily Profit and Loss"),
			fieldtype: "Select",
			options: [
				{ value: "Statement", label: __("Profit and Loss", null, "Resto Daily Profit and Loss") },
				{ value: "Cost of Goods Sold", label: __("Cost of Goods Sold", null, "Resto Daily Profit and Loss") },
			],
			default: "Statement",
		},
		{
			fieldname: "spread_fixed",
			label: __("Spread fixed expenses over the days of the month", null, "Resto Daily Profit and Loss"),
			fieldtype: "Check",
			default: 0,
			depends_on: "eval:doc.view != 'Cost of Goods Sold'",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		if (column.fieldname === "label" && data.indent) {
			value = `<span style="padding-inline-start:${data.indent * 18}px">${value}</span>`;
		}
		const negative =
			["gross_profit", "net_profit"].includes(data.key) && ["label", "amount", "pct"].includes(column.fieldname) && data.amount < 0;
		if (negative) value = `<span style="color:var(--red-600, #c0392b)">${value}</span>`;
		if (data.bold) value = `<b>${value}</b>`;
		return value;
	},
};
