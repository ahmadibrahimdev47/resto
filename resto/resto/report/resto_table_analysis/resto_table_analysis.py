"""Table use: guests, time at table, turnover and revenue per available seat hour (RevPASH)."""

import frappe
from frappe import _
from frappe.utils import cint, flt, time_diff_in_seconds

from resto.resto.report_utils import days, prepare_filters

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def execute(filters=None):
	filters = prepare_filters(filters)
	group_by = filters.get("group_by") or "Table"
	open_hours = flt(filters.get("open_hours") or 12)
	orders = frappe.db.sql(
		"""
		select o.name, o.resto_table, o.hall, o.waiter, o.guests, o.opened_at, o.closed_at,
			(select sum(si.base_net_total) from `tabSales Invoice` si
				where si.resto_order = o.name and si.docstatus = 1) as revenue
		from `tabResto Order` o
		where o.order_type = 'Dine In' and o.status = 'Paid' and o.company = %(company)s
			and date(o.opened_at) between %(from_date)s and %(to_date)s
		""",
		filters,
		as_dict=True,
	)
	tables = frappe.get_all("Resto Table", fields=["name", "seats", "hall"])
	seats = {t.name: cint(t.seats) for t in tables}
	hall_of = {t.name: t.hall for t in tables}
	n_days = days(filters)

	groups = {}
	for o in orders:
		key = {
			"Table": o.resto_table,
			"Hall": o.hall or hall_of.get(o.resto_table),
			"Waiter": frappe.utils.get_fullname(o.waiter) if o.waiter else "",
			"Weekday": _(WEEKDAYS[o.opened_at.weekday()]),
			"Hour": f"{o.opened_at.hour:02d}:00",
		}[group_by]
		g = groups.setdefault(key, {"group": key, "orders": 0, "guests": 0, "revenue": 0, "minutes": 0})
		g["orders"] += 1
		g["guests"] += cint(o.guests)
		g["revenue"] += flt(o.revenue)
		if o.closed_at:
			g["minutes"] += max(time_diff_in_seconds(o.closed_at, o.opened_at), 0) / 60

	data = []
	for g in groups.values():
		if group_by == "Table":
			seat_count, table_count = seats.get(g["group"], 0), 1
		elif group_by == "Hall":
			hall_tables = [t for t, h in hall_of.items() if h == g["group"]]
			seat_count, table_count = sum(seats.get(t, 0) for t in hall_tables), len(hall_tables)
		else:
			seat_count = table_count = 0
		seat_hours = seat_count * open_hours * n_days
		data.append(
			{
				"group": g["group"],
				"orders": g["orders"],
				"guests": g["guests"],
				"avg_guests": g["guests"] / g["orders"] if g["orders"] else 0,
				"avg_minutes": g["minutes"] / g["orders"] if g["orders"] else 0,
				"revenue": g["revenue"],
				"avg_check": g["revenue"] / g["orders"] if g["orders"] else 0,
				"per_guest": g["revenue"] / g["guests"] if g["guests"] else 0,
				"turnover": g["orders"] / (table_count * n_days) if table_count else None,
				"revpash": g["revenue"] / seat_hours if seat_hours else None,
			}
		)
	data.sort(key=lambda r: -r["revenue"])
	columns = [
		{"fieldname": "group", "label": _(group_by), "fieldtype": "Data", "width": 140},
		{"fieldname": "orders", "label": _("Orders"), "fieldtype": "Int", "width": 80},
		{"fieldname": "guests", "label": _("Guests"), "fieldtype": "Int", "width": 80},
		{"fieldname": "avg_guests", "label": _("Guests per Order"), "fieldtype": "Float", "precision": 1, "width": 110},
		{"fieldname": "avg_minutes", "label": _("Minutes at Table"), "fieldtype": "Float", "precision": 0, "width": 120},
		{"fieldname": "revenue", "label": _("Net Sales"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "avg_check", "label": _("Average Bill"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "per_guest", "label": _("Average per Guest"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "turnover", "label": _("Turns per Table per Day"), "fieldtype": "Float", "precision": 2, "width": 150},
		{"fieldname": "revpash", "label": _("RevPASH"), "fieldtype": "Currency", "width": 110},
	]
	return columns, data, _("RevPASH = net sales / (seats x opening hours x days)."), None
