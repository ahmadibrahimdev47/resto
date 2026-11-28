// Percentages show at most two decimals (D-292). Stock and recipe quantities need a float
// precision of 5 (Kg stock, gram recipes), and Frappe shows every Percent field without its own
// precision at that float precision: «61.57573%». Only the display changes: what is stored, what
// a user types and every calculation (an invoice's discount percentage that sets its rates) keep
// their full precision, so no field's precision is set (the server rounds stored values to it).
// A field that sets its own precision keeps it.
(function () {
	const TWO = 2;
	const shown = (df) => cint(df && df.precision) || TWO;
	// The value's own decimals: 12.5 shows as 12.5%, not 12.50%.
	const decimals = (value) => {
		const [mantissa, exponent] = cstr(value).toLowerCase().split("e");
		return Math.max((mantissa.split(".")[1] || "").length - cint(exponent), 0);
	};

	frappe.provide("frappe.form.formatters");
	frappe.form.formatters.Percent = function (value, docfield, options) {
		if (value === null || value === undefined) return "";
		return frappe.form.formatters._right(format_number(value, null, Math.min(shown(docfield), decimals(value))) + "%", options);
	};

	const Control = frappe.ui && frappe.ui.form && frappe.ui.form.ControlPercent;
	if (Control) {
		// What the input box shows; what is typed is still read at the float precision (parse).
		Control.prototype.format_for_input = function (value) {
			if (value === null || value === undefined || isNaN(Number(value))) return "";
			return format_number(value, this.get_number_format(), Math.min(shown(this.df), decimals(value)));
		};
	}
	frappe.provide("resto");
	resto.percent_decimals = shown;
})();
