// Demand by time (resto_demand_patterns.py). Cells are shaded by how close they are to the row's
// busiest column (to the whole table for Weekday and Hour).
(() => {
	// Labels in the "Resto" context: generic words like Average or Total keep their meaning elsewhere.
	const resto_options = (values) => values.map((v) => ({ value: v, label: __(v, null, "Resto") }));

	frappe.query_reports["Resto Demand Patterns"] = {
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
				default: frappe.datetime.add_months(frappe.datetime.add_days(frappe.datetime.get_today(), -1), -3),
				reqd: 1,
			},
			{
				fieldname: "to_date",
				label: __("To Date"),
				fieldtype: "Date",
				default: frappe.datetime.add_days(frappe.datetime.get_today(), -1), // today is not over yet
				reqd: 1,
			},
			{
				fieldname: "columns_by",
				label: __("Columns By"),
				fieldtype: "Select",
				options: resto_options(["Weekday", "Week of Month", "Day of Month", "Month", "Hour", "Weekday and Hour"]),
				default: "Weekday",
			},
			{
				fieldname: "rows_by",
				label: __("Rows By"),
				fieldtype: "Select",
				options: resto_options(["Item", "Item Group"]),
				default: "Item",
				depends_on: "eval:doc.columns_by != 'Weekday and Hour'",
			},
			{
				fieldname: "measure",
				label: __("Measure", null, "Resto"),
				fieldtype: "Select",
				options: resto_options(["Qty", "Net Sales"]),
				default: "Qty",
			},
			{
				fieldname: "show_as",
				label: __("Show As"),
				fieldtype: "Select",
				options: resto_options(["Average", "Total"]),
				default: "Average",
			},
			{
				fieldname: "item_group",
				label: __("Item Group"),
				fieldtype: "Link",
				options: "Item Group",
			},
			{
				fieldname: "item",
				label: __("Item"),
				fieldtype: "Link",
				options: "Item",
			},
			{
				fieldname: "order_type",
				label: __("Order Type"),
				fieldtype: "Select",
				options: [{ value: "", label: "" }, ...resto_options(["Dine In", "Takeaway", "Delivery", "Counter"])],
			},
			{
				fieldname: "group_variants",
				label: __("Group Sizes under the Template Item"),
				fieldtype: "Check",
				depends_on: "eval:doc.rows_by == 'Item' && doc.columns_by != 'Weekday and Hour'",
			},
		],

		formatter(value, row, column, data, default_formatter) {
			const html = default_formatter(value, row, column, data);
			if (!data || !column.fieldname.startsWith("b_") || !flt(value) || !flt(data._max)) return html;
			const ratio = Math.min(flt(value) / flt(data._max), 1);
			const alpha = (0.08 + 0.55 * ratio).toFixed(2);
			return `<div style="background:rgba(232,89,12,${alpha});margin:-4px -8px;padding:4px 8px;${ratio === 1 ? "font-weight:700;" : ""}">${html}</div>`;
		},
	};
})();
