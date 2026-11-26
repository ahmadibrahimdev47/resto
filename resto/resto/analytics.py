"""Numbers for the analytics screen: KPIs and chart series for a date range."""

import frappe
from frappe import _
from frappe.utils import flt, time_diff_in_seconds

from resto.resto.report_utils import (
	NOT_PACKAGING,
	cost_by_invoice_row,
	date_list,
	excluded_items,
	food_sales,
	invoice_condition,
	other_food_costs,
	packaging_cost,
	pct,
	prepare_filters,
	service_account,
)
from resto.resto.utils import require_role

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


@frappe.whitelist()
def get_dashboard(from_date=None, to_date=None, company=None):
	# Sales figures are for the management (D-223): the cashier's own numbers are in the shift.
	require_role("Accounts Manager")
	f = prepare_filters({"from_date": from_date, "to_date": to_date, "company": company})
	f.service_account = service_account()

	inv = frappe.db.sql(
		f"""
		select count(case when si.is_return = 0 then 1 end) as bills,
			count(case when si.is_return = 1 then 1 end) as returns,
			sum(ifnull(si.resto_guests, 0)) as guests,
			sum(si.base_total) as gross, sum(si.base_discount_amount) as discount,
			sum(si.base_net_total) as net_sales,
			sum(if(si.base_rounded_total != 0, si.base_rounded_total, si.base_grand_total)) as collected,
			sum(ifnull((select sum(t.base_tax_amount_after_discount_amount) from `tabSales Taxes and Charges` t
				where t.parent = si.name and t.parenttype = 'Sales Invoice' and t.account_head = %(service_account)s), 0)) as service
		from `tabSales Invoice` si where {invoice_condition()}
		""",
		f,
		as_dict=True,
	)[0]

	skip = excluded_items() or {""}
	f.skip = list(skip)
	cogs_rows = cost_by_invoice_row(f)
	# Takeaway packaging is not food (D-217): its cost is shown apart.
	packaging = packaging_cost(f)
	cogs = sum(cogs_rows.values()) - packaging
	extra = other_food_costs(f)
	waste, counts = extra.waste, extra.counts
	net = flt(inv.net_sales)
	food = food_sales(f)  # food cost % is on food and drinks only, like the Food Cost report
	# Per guest: only restaurant orders carry a guest count.
	resto_inv = frappe.db.sql(
		f"""select sum(if(si.base_rounded_total != 0, si.base_rounded_total, si.base_grand_total)),
			sum(ifnull(si.resto_guests, 0))
		from `tabSales Invoice` si where {invoice_condition()} and ifnull(si.resto_order, '') != ''""",
		f,
	)[0]

	voids = frappe.db.sql(
		"""select count(*), sum(oi.amount) from `tabResto Order Item` oi join `tabResto Order` o on o.name = oi.parent
		where oi.kitchen_status = 'Void' and o.company = %(company)s and date(o.opened_at) between %(from_date)s and %(to_date)s""",
		f,
	)[0]
	orders = frappe.db.sql(
		"""select opened_at, closed_at from `tabResto Order`
		where order_type = 'Dine In' and status = 'Paid' and company = %(company)s
			and date(opened_at) between %(from_date)s and %(to_date)s and closed_at is not null""",
		f,
		as_dict=True,
	)
	table_minutes = (
		sum(time_diff_in_seconds(o.closed_at, o.opened_at) for o in orders) / 60 / len(orders) if orders else 0
	)
	tickets = frappe.db.sql(
		"""select sent_at, ready_at from `tabResto Kitchen Ticket`
		where ticket_type = 'Order' and ready_at is not null and date(sent_at) between %(from_date)s and %(to_date)s""",
		f,
		as_dict=True,
	)
	kitchen_minutes = (
		sum(time_diff_in_seconds(t.ready_at, t.sent_at) for t in tickets) / 60 / len(tickets) if tickets else 0
	)
	open_now = frappe.db.count("Resto Order", {"status": ("in", ("Open", "Bill Requested")), "company": f.company, "staff_meal": 0})

	kpis = {
		"net_sales": net,
		"collected": flt(inv.collected),
		"bills": inv.bills or 0,
		"returns": inv.returns or 0,
		"guests": inv.guests or 0,
		"avg_bill": flt(inv.collected) / inv.bills if inv.bills else 0,
		"avg_guest": flt(resto_inv[0]) / flt(resto_inv[1]) if flt(resto_inv[1]) else 0,
		"discount": flt(inv.discount),
		"discount_pct": pct(inv.discount, inv.gross),
		"service": flt(inv.service),
		"cogs": cogs,
		"packaging_cost": packaging,
		"food_sales": food,
		"food_cost_pct": pct(cogs, food),
		"waste": waste,
		"waste_pct": pct(waste, food),
		"count_differences": counts,
		"prep_adjustments": extra.prep,
		"total_food_cost_pct": pct(cogs + extra.total, food),
		"voids": voids[0] or 0,
		"void_amount": flt(voids[1]),
		"table_minutes": table_minutes,
		"kitchen_minutes": kitchen_minutes,
		"open_orders": open_now,
		"food_cost_alert": flt(frappe.db.get_single_value("Resto Settings", "food_cost_alert_percent")),
	}

	by_day = dict(
		frappe.db.sql(
			f"select si.posting_date, sum(si.base_net_total) from `tabSales Invoice` si where {invoice_condition()} group by si.posting_date",
			f,
		)
	)
	days = date_list(f)
	by_hour = dict(
		frappe.db.sql(
			f"select hour(si.posting_time), sum(si.base_net_total) from `tabSales Invoice` si where {invoice_condition()} group by 1",
			f,
		)
	)
	by_weekday = dict(
		frappe.db.sql(
			f"select dayofweek(si.posting_date), sum(si.base_net_total) from `tabSales Invoice` si where {invoice_condition()} group by 1",
			f,
		)
	)
	by_type = frappe.db.sql(
		f"""select ifnull(nullif(si.resto_order_type, ''), 'Counter'), sum(si.base_net_total)
		from `tabSales Invoice` si where {invoice_condition()} group by 1 order by 2 desc""",
		f,
	)
	top_items = frappe.db.sql(
		f"""select max(sii.item_name), sum(sii.stock_qty), sum(sii.base_net_amount)
		from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
		where {invoice_condition()} and sii.item_code not in %(skip)s and {NOT_PACKAGING}
		group by sii.item_code order by 3 desc limit 10""",
		f,
	)
	by_group = frappe.db.sql(
		f"""select sii.item_group, sum(sii.base_net_amount)
		from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
		where {invoice_condition()} and sii.item_code not in %(skip)s and {NOT_PACKAGING} group by 1 order by 2 desc limit 8""",
		f,
	)
	by_waiter = frappe.db.sql(
		f"""select ifnull(si.resto_waiter, si.owner), sum(si.base_net_total), count(*)
		from `tabSales Invoice` si where {invoice_condition()} group by 1 order by 2 desc limit 10""",
		f,
	)
	hours = list(range(24))
	return {
		"filters": {"from_date": str(f.from_date), "to_date": str(f.to_date), "company": f.company},
		"kpis": kpis,
		"charts": {
			"by_day": {"labels": [str(d) for d in days], "values": [flt(by_day.get(d)) for d in days]},
			"by_hour": {
				"labels": [f"{h:02d}" for h in hours if by_hour.get(h)],
				"values": [flt(by_hour.get(h)) for h in hours if by_hour.get(h)],
			},
			"by_weekday": {
				"labels": [_(WEEKDAYS[i - 1]) for i in range(1, 8)],
				"values": [flt(by_weekday.get(i)) for i in range(1, 8)],
			},
			"by_type": {"labels": [_(r[0], context="Resto") for r in by_type], "values": [flt(r[1]) for r in by_type]},
			"top_items": {"labels": [r[0] for r in top_items], "values": [flt(r[2]) for r in top_items], "qty": [flt(r[1]) for r in top_items]},
			"by_group": {"labels": [_(r[0], context="Resto") for r in by_group], "values": [flt(r[1]) for r in by_group]},
			"by_waiter": {
				"labels": [frappe.utils.get_fullname(r[0]) for r in by_waiter],
				"values": [flt(r[1]) for r in by_waiter],
			},
		},
	}
