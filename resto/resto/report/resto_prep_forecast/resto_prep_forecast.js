// Prep forecast (resto_prep_forecast.py): expected dishes for a day and what the kitchen needs.
(() => {
	// Generic words: the Resto context keeps them from changing core screens.
	const options = (values) => values.map((v) => ({ value: v, label: __(v, null, "Resto") }));
	const METHOD = "resto.resto.report.resto_prep_forecast.resto_prep_forecast.";
	// Tomorrow as a business day: before "Business Day Starts At" today is still yesterday.
	const business_tomorrow = () => {
		const hours = frappe.boot.resto?.business_day_start_hour || 0;
		const today = moment(frappe.datetime.now_datetime()).subtract(hours, "hours").format("YYYY-MM-DD");
		return frappe.datetime.add_days(today, 1);
	};

	function make(report, method, doctype) {
		frappe.call({
			method: METHOD + method,
			args: { filters: report.get_filter_values() },
			freeze: true,
			callback: (r) => {
				if (r.message) frappe.set_route("Form", doctype, r.message);
			},
		});
	}

	frappe.query_reports["Resto Prep Forecast"] = {
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
				fieldname: "target_date",
				label: __("Forecast Date"),
				fieldtype: "Date",
				default: business_tomorrow(),
				reqd: 1,
			},
			{
				fieldname: "weeks",
				label: __("Past Weeks"),
				fieldtype: "Int",
				default: 6,
			},
			{
				fieldname: "method",
				label: __("Method", null, "Resto"),
				fieldtype: "Select",
				options: options(["Average", "Weighted Average", "Average without Highest and Lowest"]),
				default: "Average",
			},
			{
				fieldname: "safety_margin",
				label: __("Safety Margin %"),
				fieldtype: "Percent",
				default: 10,
				description: __("Added on top of the expected quantity."),
			},
			{
				fieldname: "view",
				label: __("View", null, "Resto"),
				fieldtype: "Select",
				options: [
					{ value: "Dishes", label: __("Expected Dishes") },
					{ value: "Ingredients", label: __("Needed Ingredients") },
				],
				default: "Dishes",
			},
			{
				fieldname: "item_group",
				label: __("Item Group"),
				fieldtype: "Link",
				options: "Item Group",
			},
		],

		onload(report) {
			if (frappe.model.can_create("Resto Prep")) {
				report.page.add_inner_button(__("Create Daily Prep"), () => make(report, "make_prep", "Resto Prep"));
			}
			if (frappe.model.can_create("Stock Entry")) {
				report.page.add_inner_button(__("Create Material Transfer"), () => make(report, "make_transfer", "Stock Entry"));
			}
		},

		formatter(value, row, column, data, default_formatter) {
			const html = default_formatter(value, row, column, data);
			if (!data) return html;
			// What needs action stands out.
			if (["to_prepare", "to_transfer"].includes(column.fieldname) && flt(value) > 0) return `<b style="color:var(--orange-600)">${html}</b>`;
			if (column.fieldname === "to_buy" && flt(value) > 0) return `<b style="color:var(--red-600)">${html}</b>`;
			if (column.fieldname === "expected" || column.fieldname === "need") return `<b>${html}</b>`;
			// Negative stock: sales were posted before the stock was.
			if (["in_kitchen", "in_main"].includes(column.fieldname) && flt(value) < 0) return `<span style="color:var(--red-600)">${html}</span>`;
			return html;
		},
	};
})();
