"""Sales history by business day, hour and item, shared by the demand reports.

Source: submitted Sales Invoices of the company (POSNext, the floor and the desk all end in a
Sales Invoice). Returns are netted back onto the day of the invoice they return, so a line rung
by mistake and returned does not count as demand. The delivery fee item is left out.
Quantities are in the item's stock unit.

Time of a sale: for a floor bill, when its order was opened (that is when the kitchen works on
it); otherwise the invoice posting time. Business day: sales before "Business Day Starts At"
(Resto Settings) count for the day before, for a restaurant that stays open past midnight.
"""

import frappe
from frappe.utils import add_days, cint, getdate

from resto.resto.report_utils import excluded_items
from resto.resto.utils import get_settings

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# The invoice a line belongs to for its time: the original invoice of a return.
_SOLD_AT = """coalesce(ro.opened_at, timestamp(coalesce(orig.posting_date, si.posting_date),
	coalesce(orig.posting_time, si.posting_time)))"""
_JOINS = """left join `tabSales Invoice` orig on si.is_return = 1 and orig.name = si.return_against
	left join `tabResto Order` ro on ro.name = coalesce(nullif(si.resto_order, ''), nullif(orig.resto_order, ''))"""
# Rows posted near the range are fetched; the business day decides (an order opened before
# midnight, a bill paid after it, a return posted later).
_NEAR = """((si.is_return = 0 and si.posting_date between %(near_from)s and %(near_to)s)
	or (si.is_return = 1 and orig.posting_date between %(near_from)s and %(near_to)s))"""


def day_start_hour():
	return min(max(cint(get_settings().get("business_day_start_hour")), 0), 12)


def _values(company, from_date, to_date):
	from_date, to_date = getdate(from_date), getdate(to_date)
	return {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"near_from": add_days(from_date, -1),
		"near_to": add_days(to_date, 1),
		"shift": day_start_hour(),
	}


def sales_by_day(company, from_date, to_date, item_group=None, item=None, order_type=None, group_variants=False,
	by_hour=False, dates=None):
	"""Rows of (posting_date = business day, hour or None, item, item_name, item_group, qty, net)."""
	values = _values(company, from_date, to_date)
	cond = ["sii.item_code is not null"]
	skip = list(excluded_items())
	if skip:
		cond.append("sii.item_code not in %(skip)s")
		values["skip"] = skip
	if item_group:
		lft, rgt = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"]) or (0, 0)
		cond.append("sii.item_group in (select name from `tabItem Group` where lft >= %(ig_lft)s and rgt <= %(ig_rgt)s)")
		values.update(ig_lft=lft, ig_rgt=rgt)
	if item:
		# A template also brings its sizes (variants).
		cond.append("(sii.item_code = %(item)s or i.variant_of = %(item)s)")
		values["item"] = item
	order_type_expr = "coalesce(nullif(si.resto_order_type, ''), nullif(orig.resto_order_type, ''), '')"
	if order_type == "Counter":
		cond.append(f"{order_type_expr} = ''")
	elif order_type:
		cond.append(f"{order_type_expr} = %(order_type)s")
		values["order_type"] = order_type
	outer = "bday between %(from_date)s and %(to_date)s"
	if dates:
		outer += " and bday in %(dates)s"
		values["dates"] = [getdate(d) for d in dates]

	key = "ifnull(nullif(i.variant_of, ''), sii.item_code)" if group_variants else "sii.item_code"
	rows = frappe.db.sql(
		f"""
		select bday as posting_date, {"hr" if by_hour else "null"} as hour, item,
			max(item_group) as item_group, sum(qty) as qty, sum(net) as net
		from (
			select date({_SOLD_AT} - interval %(shift)s hour) as bday, hour({_SOLD_AT}) as hr,
				{key} as item, sii.item_group, sii.stock_qty as qty, sii.base_net_amount as net
			from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			left join `tabItem` i on i.name = sii.item_code
			{_JOINS}
			where si.docstatus = 1 and si.company = %(company)s and {_NEAR} and {" and ".join(cond)}
		) t
		where {outer}
		group by bday{", hr" if by_hour else ""}, item
		""",
		values,
		as_dict=True,
	)
	names = dict(frappe.get_all("Item", filters={"name": ["in", list({r.item for r in rows}) or [""]]},
		fields=["name", "item_name"], as_list=True))
	for r in rows:
		r.item_name = names.get(r.item) or r.item
	return rows


def open_days(company, from_date, to_date):
	"""Business days with at least one sale: a closed day does not count when averaging."""
	values = _values(company, from_date, to_date)
	return [
		getdate(d)
		for (d,) in frappe.db.sql(
			f"""
			select distinct bday from (
				select date({_SOLD_AT} - interval %(shift)s hour) as bday
				from `tabSales Invoice` si
				{_JOINS}
				where si.docstatus = 1 and si.is_return = 0 and si.company = %(company)s and {_NEAR}
			) t
			where bday between %(from_date)s and %(to_date)s
			order by bday
			""",
			values,
		)
	]


def first_sale_dates(company, items):
	"""The first day each item was ever sold: a new dish is averaged only over the days since."""
	if not items:
		return {}
	# The same business day as the history (order time, day start hour).
	return {
		item: getdate(d)
		for item, d in frappe.db.sql(
			f"""select sii.item_code, min(date({_SOLD_AT} - interval %(shift)s hour))
			from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			{_JOINS}
			where si.docstatus = 1 and si.is_return = 0 and si.company = %(company)s and sii.item_code in %(items)s
			group by sii.item_code""",
			{"shift": day_start_hour(), "company": company, "items": list(items)},
		)
	}


def first_weekday():
	"""0 = Monday … 6 = Sunday, from System Settings (Saturday for most Egyptian sites)."""
	first = frappe.db.get_single_value("System Settings", "first_day_of_the_week") or "Sunday"
	return WEEKDAYS.index(first) if first in WEEKDAYS else 6


def week_of_month(d):
	"""1 for days 1–7, 2 for 8–14 … 5 for 29–31."""
	return (cint(getdate(d).day) - 1) // 7 + 1


def business_today():
	"""Today as a business day: before "Business Day Starts At" it is still yesterday."""
	from datetime import timedelta

	from frappe.utils import now_datetime

	return getdate(now_datetime() - timedelta(hours=day_start_hour()))
