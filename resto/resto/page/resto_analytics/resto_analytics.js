// Restaurant analytics: KPIs and charts for a date range, with links to the detailed reports.

frappe.pages["resto-analytics"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Restaurant Analytics"), single_column: true });
	wrapper.resto_analytics = new RestoAnalytics(page);
};

class RestoAnalytics {
	constructor(page) {
		this.page = page;
		this.company = page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.load(),
		});
		page.add_inner_button(__("Refresh"), () => this.load());
		// The period in the page itself (plain date inputs): the page's own filter bar did not show
		// its date fields on every version.
		const today = frappe.datetime.get_today();
		const periods = [
			["today", __("Today", null, "Resto")],
			["yesterday", __("Yesterday", null, "Resto")],
			["7", __("Last 7 Days")],
			["30", __("Last 30 Days", null, "Resto")],
			["month", __("This Month", null, "Resto")],
			["last_month", __("Last Month", null, "Resto")],
			["custom", __("Custom")],
		];
		$(page.body).html(`<style>${RestoAnalytics.css()}</style>
			<div class="ra-filters">
				<label>${__("Period")}<select class="form-control input-sm ra-range">${periods
					.map(([v, l]) => `<option value="${v}">${frappe.utils.escape_html(l)}</option>`)
					.join("")}</select></label>
				<label>${__("From Date")}<input type="date" class="form-control input-sm ra-from" value="${today}"></label>
				<label>${__("To Date")}<input type="date" class="form-control input-sm ra-to" value="${today}"></label>
			</div>
			<div class="ra-kpis"></div>
			<div class="ra-charts"></div>
			<div class="ra-reports"></div>`);
		this.$range = $(page.body).find(".ra-range");
		this.$from = $(page.body).find(".ra-from");
		this.$to = $(page.body).find(".ra-to");
		this.$range.on("change", () => this.set_range());
		this.$from.add(this.$to).on("change", () => {
			this.$range.val("custom");
			this.load();
		});
		this.$kpis = $(page.body).find(".ra-kpis");
		this.$charts = $(page.body).find(".ra-charts");
		this.render_reports($(page.body).find(".ra-reports"));
		this.load();
	}

	set_range() {
		const v = this.$range.val();
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
		this.$from.val(from);
		this.$to.val(to);
		this.load();
	}

	load() {
		if (!this.$from) return; // the company field set before the page is built
		const from = this.$from.val(), to = this.$to.val();
		if (!from || !to) return;
		if (from > to) {
			frappe.show_alert({ message: __("From Date must be before To Date"), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "resto.resto.analytics.get_dashboard",
			args: { from_date: from, to_date: to, company: this.company.get_value() },
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
			[__("Packaging and Consumables"), money(k.packaging_cost), __("Not in the food cost"), ""],
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
			// A pie, not a donut: the donut's total in its hole ran over the ring for a large amount.
			["by_group", __("Sales by Item Group"), "pie"],
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
			const round = type === "pie";
			// A round chart's legend is drawn under it, inside the same height: room for it, and the
			// smaller slices joined here into one named in the user's language (the chart's own
			// joining names it "Rest", untranslated).
			const { labels, values } = round ? RestoAnalytics.top_slices(data, 6) : data;
			new frappe.Chart(el, {
				data: { labels, datasets: [{ name: title, values }] },
				type,
				height: round ? 320 : 240,
				maxSlices: round ? 7 : undefined,
				maxLegendPoints: round ? 7 : undefined,
				truncateLegends: round ? 1 : undefined,
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
		// Only the reports this user may open (the report's roles).
		const allowed = (frappe.boot.user && frappe.boot.user.all_reports) || null;
		$el.html(
			`<h5>${__("Detailed Reports")}</h5><div class="ra-links">${reports
				.filter((r) => !allowed || allowed[r])
				.map((r) => `<a class="btn btn-default btn-sm" href="/app/query-report/${encodeURIComponent(r)}">${__(r)}</a>`)
				.join("")}</div>`
		);
	}

	static top_slices(data, keep) {
		// The largest keep - 1 slices, and the rest as one «Other».
		const rows = data.labels.map((label, i) => [label, data.values[i] || 0]).sort((a, b) => b[1] - a[1]);
		if (rows.length <= keep) return { labels: rows.map((r) => r[0]), values: rows.map((r) => r[1]) };
		const top = rows.slice(0, keep - 1);
		const rest = rows.slice(keep - 1).reduce((sum, r) => sum + r[1], 0);
		return { labels: [...top.map((r) => r[0]), __("Other", null, "Resto")], values: [...top.map((r) => r[1]), rest] };
	}

	static css() {
		return `
		.ra-filters { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; margin-bottom:12px; }
		.ra-filters label { display:flex; flex-direction:column; gap:3px; font-size:12px; color:var(--text-muted); margin:0; }
		.ra-filters .form-control { min-width:150px; }
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
