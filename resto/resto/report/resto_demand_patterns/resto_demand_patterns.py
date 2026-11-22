"""Demand by time: which dishes sell on which weekday, week of the month, day of the month,
month or hour, by quantity or net sales, as a total or an average per day.

Days are business days (see resto/resto/demand.py). An average divides each column by the
open days (days with at least one sale) that fall in it, so every average is "per day": Monday is
an average Monday, Week 5 an average day of days 29–31, March an average day in March, 13:00 the
average 13:00 hour of a day.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, add_months, flt, getdate, nowdate

from resto.resto.demand import WEEKDAYS, first_weekday, open_days, sales_by_day, week_of_month
from resto.resto.utils import get_settings

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
	"October", "November", "December"]
COLUMNS_BY = ("Weekday", "Week of Month", "Day of Month", "Month", "Hour", "Weekday and Hour")


def execute(filters=None):
	f = prepare(filters)
	sales = sales_by_day(
		f.company, f.from_date, f.to_date, item_group=f.item_group, item=f.item, order_type=f.order_type,
		group_variants=f.group_variants, by_hour=f.columns_by in ("Hour", "Weekday and Hour"),
	)
	days = open_days(f.company, f.from_date, f.to_date)
	if not sales:
		return [], [], _("No sales in this period for these filters.")
	columns, data = build(f, sales, days)
	return columns, data, None, chart(f, columns, data), summary(f, columns, data, days)


def prepare(filters):
	f = frappe._dict(filters or {})
	f.company = f.company or get_settings().company
	# Today is not over yet: it would pull its weekday down.
	f.to_date = getdate(f.to_date or add_days(nowdate(), -1))
	f.from_date = getdate(f.from_date or add_months(f.to_date, -3))
	if f.from_date > f.to_date:
		frappe.throw(_("From Date must be before To Date"))
	f.columns_by = f.columns_by if f.columns_by in COLUMNS_BY else "Weekday"
	f.rows_by = "Item Group" if f.rows_by == "Item Group" else "Item"
	f.measure = "net" if f.measure == "Net Sales" else "qty"
	f.average = (f.show_as or "Average") == "Average"
	f.group_variants = bool(frappe.utils.cint(f.group_variants)) and f.rows_by == "Item"
	return f


# --------------------------------------------------------------------------- buckets


def weekday_order():
	first = first_weekday()
	return [(first + i) % 7 for i in range(7)]


def day_key(columns_by, d):
	"""The column a day falls in (for Hour and Weekday and Hour: the weekday row, or every day)."""
	d = getdate(d)
	if columns_by == "Week of Month":
		return week_of_month(d)
	if columns_by == "Day of Month":
		return d.day
	if columns_by == "Month":
		return d.month
	if columns_by == "Hour":
		return None
	return d.weekday()  # Weekday, and the rows of Weekday and Hour


def buckets(columns_by, sales):
	"""Ordered (key, label) of the columns."""
	if columns_by == "Weekday":
		return [(k, _(WEEKDAYS[k])) for k in weekday_order()]
	if columns_by == "Week of Month":
		return [(k, _("Week {0}").format(k)) for k in range(1, 6)]
	if columns_by == "Day of Month":
		return [(k, str(k)) for k in range(1, 32)]
	if columns_by == "Month":
		return [(k, _(MONTHS[k - 1], context="Resto")) for k in range(1, 13)]
	hours = [int(r.hour or 0) for r in sales]
	return [(h, f"{h:02d}:00") for h in range(min(hours), max(hours) + 1)]


def open_days_by(columns_by, days):
	count = defaultdict(int)
	for d in days:
		count[day_key(columns_by, d)] += 1
	return count


# --------------------------------------------------------------------------- table


def build(f, sales, days):
	cols = buckets(f.columns_by, sales)
	per_day = open_days_by(f.columns_by, days)
	weekday_rows = f.columns_by == "Weekday and Hour"

	sums, names = defaultdict(lambda: defaultdict(float)), {}
	for r in sales:
		if weekday_rows:
			row = getdate(r.posting_date).weekday()
		elif f.rows_by == "Item Group":
			row = r.item_group
		else:
			row = r.item
			names[row] = r.item_name
		column = int(r.hour or 0) if f.columns_by in ("Hour", "Weekday and Hour") else day_key(f.columns_by, r.posting_date)
		sums[row][column] += flt(r[f.measure])

	def divide(value, n):
		return value / n if f.average and n else (0 if f.average else value)

	data = []
	for row, values in sums.items():
		d = {"row": row}
		if weekday_rows:
			# Rows are weekdays: each cell is an average of that weekday's open days.
			n = per_day.get(row, 0)
			d["row_label"] = _(WEEKDAYS[row])
			for key, _label in cols:
				d[f"b_{key}"] = divide(values.get(key, 0), n)
			d["total"] = divide(sum(values.values()), n)
		else:
			if f.rows_by == "Item":
				d["item_name"] = names.get(row)
			for key, _label in cols:
				n = per_day.get(None if f.columns_by == "Hour" else key, 0)
				d[f"b_{key}"] = divide(values.get(key, 0), n)
			d["total"] = divide(sum(values.values()), len(days))
		peak = max(cols, key=lambda c: d[f"b_{c[0]}"])
		d["peak"] = peak[1] if d[f"b_{peak[0]}"] > 0 else ""
		d["_max"] = max(d[f"b_{k}"] for k, _l in cols)
		data.append(d)

	if weekday_rows:
		order = {k: i for i, k in enumerate(weekday_order())}
		data.sort(key=lambda d: order[d["row"]])
		table_max = max(d["_max"] for d in data)
		for d in data:
			d["_max"] = table_max  # one heat scale for the whole week
	else:
		data.sort(key=lambda d: -d["total"])

	fieldtype = "Currency" if f.measure == "net" else "Float"
	columns = []
	if weekday_rows:
		columns.append({"fieldname": "row_label", "label": _("Weekday"), "fieldtype": "Data", "width": 110})
	elif f.rows_by == "Item Group":
		columns.append({"fieldname": "row", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 160})
	else:
		columns += [
			{"fieldname": "row", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
			{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170},
		]
	for key, label in cols:
		columns.append({"fieldname": f"b_{key}", "label": label, "fieldtype": fieldtype, "precision": 1, "width": 90 if len(cols) <= 12 else 70})
	columns += [
		{"fieldname": "total", "label": _("Daily Average") if f.average else _("Total", context="Resto"), "fieldtype": fieldtype, "precision": 1, "width": 110},
		{"fieldname": "peak", "label": _("Busiest"), "fieldtype": "Data", "width": 110},
	]
	return columns, data


def bucket_columns(columns):
	return [c for c in columns if c["fieldname"].startswith("b_")]


def row_name(d):
	return d.get("row_label") or d.get("item_name") or d.get("row")


def chart(f, columns, data):
	cols = bucket_columns(columns)
	top = data if f.columns_by == "Weekday and Hour" else data[:5]
	return {
		"data": {
			"labels": [c["label"] for c in cols],
			"datasets": [{"name": str(row_name(d)), "values": [flt(d[c["fieldname"]], 2) for c in cols]} for d in top],
		},
		"type": "line" if len(cols) > 12 else "bar",
	}


def summary(f, columns, data, days):
	cols = bucket_columns(columns)
	datatype = "Currency" if f.measure == "net" else "Float"
	cards = [{"label": _("Open Days"), "value": len(days), "datatype": "Int"}]
	if f.columns_by == "Weekday and Hour":
		# The busiest hour of the week.
		best = max(((d, c) for d in data for c in cols), key=lambda x: flt(x[0][x[1]["fieldname"]]))
		cards.append({"label": _("Busiest: {0}").format(f"{best[0]['row_label']} {best[1]['label']}"),
			"value": best[0][best[1]["fieldname"]], "datatype": datatype, "indicator": "Green"})
		return cards
	totals = [(c, sum(flt(d[c["fieldname"]]) for d in data)) for c in cols]
	busiest = max(totals, key=lambda t: t[1])
	cards.append({"label": _("Busiest: {0}").format(busiest[0]["label"]), "value": busiest[1], "datatype": datatype, "indicator": "Green"})
	quiet = [t for t in totals if t[1] > 0]
	if quiet:
		low = min(quiet, key=lambda t: t[1])
		cards.append({"label": _("Quietest: {0}").format(low[0]["label"]), "value": low[1], "datatype": datatype, "indicator": "Orange"})
	cards.append({"label": _("Top Seller"), "value": str(row_name(data[0])), "datatype": "Data", "indicator": "Blue"})
	if len(cols) <= 7:
		# The best seller of each column: what to prepare more of on that day.
		for c in cols:
			best = max(data, key=lambda d: flt(d[c["fieldname"]]))
			if flt(best[c["fieldname"]]) > 0:
				cards.append({"label": _("Top on {0}").format(c["label"]), "value": str(row_name(best)), "datatype": "Data"})
	return cards
