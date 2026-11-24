// Restaurant analytics: KPIs and charts for a date range, with links to the detailed reports.

frappe.pages["resto-analytics"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Restaurant Analytics"), single_column: true });
	wrapper.resto_analytics = new RestoAnalytics(page);
};

class RestoAnalytics {
	constructor(page) {
		this.page = page;
		const today = frappe.datetime.get_today();
		this.range = page.add_field({
			fieldname: "range",
			label: __("Period"),
			fieldtype: "Select",
			options: [
				{ value: "today", label: __("Today", null, "Resto") },
				{ value: "yesterday", label: __("Yesterday", null, "Resto") },
				{ value: "7", label: __("Last 7 Days") },
				{ value: "30", label: __("Last 30 Days", null, "Resto") },
				{ value: "month", label: __("This Month", null, "Resto") },
				{ value: "last_month", label: __("Last Month", null, "Resto") },
				{ value: "custom", label: __("Custom") },
			],
			default: "today",
			change: () => this.set_range(),
		});
		this.from = page.add_field({ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: today, change: () => this.load() });
		this.to = page.add_field({ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: today, change: () => this.load() });
		this.company = page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.load(),
		});
		page.add_inner_button(__("Refresh"), () => this.load());
		$(page.body).html(`<style>${RestoAnalytics.css()}</style>
			<div class="ra-kpis"></div>
			<div class="ra-charts"></div>
			<div class="ra-reports"></div>`);
		this.$kpis = $(page.body).find(".ra-kpis");
		this.$charts = $(page.body).find(".ra-charts");
		this.render_reports($(page.body).find(".ra-reports"));
		this.load();
	}

	set_range() {
		const v = this.range.get_value();
		const today = frappe.datetime.get_today();
		const add = frappe.datetime.add_days;
		let from = today, to = today;
		if (v === "yesterday") from = to = add(today, -1);
		else if (v === "7") from = add(today, -6);
		else if (v === "30") from = add(today, -29);
		else if (v === "month") from = frappe.datetime.month_start();
		else if (v === "last_month") {
			from = frappe.datetime.add_months(frappe.datetime.month_start(), -1);
			to = add(frappe.datetime.month_start(), -1);
		} else return;
		this._silent = true;
		this.from.set_value(from);
		this.to.set_value(to).then(() => { this._silent = false; this.load(); });
	}

	load() {
		if (this._silent) return;
		frappe.call({
			method: "resto.resto.analytics.get_dashboard",
			args: { from_date: this.from.get_value(), to_date: this.to.get_value(), company: this.company.get_value() },
			callback: (r) => this.render(r.message),
		});
	}

	render(d) {
		const k = d.kpis;
		const money = (v) => format_currency(v || 0);
		const p = (v) => `${(v || 0).toFixed(1)}%`;
		const fc_bad = k.food_cost_alert && k.total_food_cost_pct > k.food_cost_alert;
		const cards = [
			[__("Net Sales"), money(k.net_sales), `${__("Collected")}: ${money(k.collected)}`, "blue"],
			[__("Bills"), k.bills, `${__("Returns")}: ${k.returns}`, ""],
			[__("Guests"), k.guests, `${__("Open Orders Now")}: ${k.open_orders}`, ""],
			[__("Average Bill"), money(k.avg_bill), `${__("Per Guest")}: ${money(k.avg_guest)}`, ""],
			[__("Food Cost %"), p(k.food_cost_pct), `${__("Cost of Food Sold")}: ${money(k.cogs)}`, k.food_cost_pct > k.food_cost_alert ? "red" : "green"],
			[__("Total Food Cost %"), p(k.total_food_cost_pct), __("With waste and count differences"), fc_bad ? "red" : "green"],
			[__("Waste"), money(k.waste), `${p(k.waste_pct)} ${__("of sales")}`, "orange"],
			[__("Discounts"), money(k.discount), `${p(k.discount_pct)} ${__("of items total")}`, "orange"],
			[__("Service Charge"), money(k.service), "", ""],
			[__("Voids"), k.voids, money(k.void_amount), k.voids ? "red" : ""],
			[__("Minutes at Table"), Math.round(k.table_minutes), __("Average for dine-in"), ""],
			[__("Kitchen Time"), `${(k.kitchen_minutes || 0).toFixed(1)}′`, __("Sent to ready, average"), ""],
		];
		this.$kpis.html(
			cards
				.map(
					([label, value, sub, color]) => `<div class="ra-card ${color}">
					<div class="ra-label">${label}</div><div class="ra-value">${value}</div><div class="ra-sub">${sub || ""}</div></div>`
				)
				.join("")
		);
		const c = d.charts;
		const blocks = [
			["by_day", __("Net Sales by Day"), "line"],
			["by_hour", __("Net Sales by Hour"), "bar"],
			["by_weekday", __("Net Sales by Weekday"), "bar"],
			["by_type", __("Sales by Order Type"), "pie"],
			["top_items", __("Top 10 Items"), "bar"],
			["by_group", __("Sales by Item Group"), "donut"],
			["by_waiter", __("Sales by Waiter"), "bar"],
		];
		this.$charts.html(blocks.map(([key, title]) => `<div class="ra-chart"><h6>${title}</h6><div data-chart="${key}"></div></div>`).join(""));
		blocks.forEach(([key, title, type]) => {
			const data = c[key];
			const el = this.$charts.find(`[data-chart="${key}"]`)[0];
			if (!data.labels.length || !data.values.some((v) => v)) {
				$(el).html(`<div class="text-muted ra-empty">${__("No data")}</div>`);
				return;
			}
			new frappe.Chart(el, {
				data: { labels: data.labels, datasets: [{ name: title, values: data.values }] },
				type,
				height: 240,
				colors: ["#E8590C", "#2490EF", "#29CD42", "#7575FF", "#FFA00A", "#ECAD4B", "#00BAE5", "#FF5858"],
				axisOptions: { xIsSeries: type === "line" ? 1 : 0 },
				tooltipOptions: { formatTooltipY: (v) => format_currency(v) },
			});
		});
	}

	render_reports($el) {
		const reports = [
			"Resto Sales Analysis",
			"Resto Demand Patterns",
			"Resto Prep Forecast",
			"Resto Menu Engineering",
			"Resto Food Cost",
			"Resto Kitchen Times",
			"Resto Table Analysis",
			"Resto Voids and Discounts",
			"Resto Kitchen vs Invoices",
			"Resto Waste Analysis",
			"Resto Customer Insights",
			"Resto Purchase Price Changes",
			"Resto Negative Prepared Stock",
		];
		$el.html(
			`<h5>${__("Detailed Reports")}</h5><div class="ra-links">${reports
				.map((r) => `<a class="btn btn-default btn-sm" href="/app/query-report/${encodeURIComponent(r)}">${__(r)}</a>`)
				.join("")}</div>`
		);
	}

	static css() {
		return `
		.ra-kpis { display:grid; grid-template-columns:repeat(auto-fill, minmax(190px,1fr)); gap:10px; margin-bottom:16px; }
		.ra-card { border:1px solid var(--border-color); border-radius:10px; padding:12px; background:var(--card-bg); border-inline-start:4px solid var(--gray-400); }
		.ra-card.blue { border-inline-start-color:#2490EF } .ra-card.green { border-inline-start-color:#29CD42 }
		.ra-card.red { border-inline-start-color:#E24C4C } .ra-card.orange { border-inline-start-color:#FFA00A }
		.ra-label { font-size:12px; color:var(--text-muted); }
		.ra-value { font-size:22px; font-weight:700; }
		.ra-sub { font-size:11px; color:var(--text-muted); }
		.ra-charts { display:grid; grid-template-columns:repeat(auto-fill, minmax(420px,1fr)); gap:12px; }
		@media (max-width: 600px) { .ra-charts { grid-template-columns:1fr; } }
		.ra-chart { border:1px solid var(--border-color); border-radius:10px; padding:10px; background:var(--card-bg); }
		.ra-empty { padding:40px; text-align:center; }
		.ra-reports { margin-top:18px; }
		.ra-links { display:flex; flex-wrap:wrap; gap:6px; }`;
	}
}
