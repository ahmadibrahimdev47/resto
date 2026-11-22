"""Shared queries for Resto reports and the analytics screen. All amounts are in company currency."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

from resto.resto.utils import get_settings


def prepare_filters(filters):
	filters = frappe._dict(filters or {})
	filters.company = filters.company or get_settings().company
	filters.to_date = getdate(filters.to_date or nowdate())
	filters.from_date = getdate(filters.from_date or get_first_day(filters.to_date))
	if filters.from_date > filters.to_date:
		frappe.throw(_("From Date must be before To Date"))
	return filters


def days(filters):
	return (getdate(filters.to_date) - getdate(filters.from_date)).days + 1


def service_account():
	return get_settings().service_charge_account or ""


def invoice_condition(alias="si"):
	return f"""{alias}.docstatus = 1 and {alias}.company = %(company)s
		and {alias}.posting_date between %(from_date)s and %(to_date)s"""


def cost_by_invoice_row(filters):
	"""Actual cost of goods sold per Sales Invoice Item row, from the stock ledger.

	A menu dish (Product Bundle) has no stock of its own: its components are posted against
	Packed Item rows, which point back to the invoice row through parent_detail_docname.
	"""
	rows = frappe.db.sql(
		"""
		select coalesce(pi.parent_detail_docname, sle.voucher_detail_no) as row_name,
			sum(-sle.stock_value_difference) as cost
		from `tabStock Ledger Entry` sle
		left join `tabPacked Item` pi
			on pi.name = sle.voucher_detail_no and pi.parenttype = 'Sales Invoice'
		where sle.voucher_type = 'Sales Invoice' and sle.is_cancelled = 0
			and sle.company = %(company)s
			and sle.posting_date between %(from_date)s and %(to_date)s
		group by row_name
		""",
		filters,
		as_dict=True,
	)
	return {r.row_name: flt(r.cost) for r in rows}


def sold_rows(filters, item_group=None):
	"""Sales Invoice Item rows of the period with net amount (after invoice discount)."""
	cond = ""
	if item_group:
		lft, rgt = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"])
		filters["ig_lft"], filters["ig_rgt"] = lft, rgt
		cond = """and sii.item_group in (select name from `tabItem Group`
			where lft >= %(ig_lft)s and rgt <= %(ig_rgt)s)"""
	return frappe.db.sql(
		f"""
		select sii.name, sii.item_code, sii.item_name, sii.item_group, sii.stock_qty as qty,
			sii.base_net_amount as net_amount, si.name as invoice, si.posting_date,
			si.resto_order_type, si.is_return
		from `tabSales Invoice Item` sii
		join `tabSales Invoice` si on si.name = sii.parent
		where {invoice_condition()} {cond}
		""",
		filters,
		as_dict=True,
	)


def recipe_costs():
	"""Current recipe cost per stock unit of each item that has an active recipe."""
	return {
		r.item: flt(r.cost_per_unit)
		for r in frappe.get_all("Resto Recipe", filters={"is_active": 1}, fields=["item", "cost_per_unit"])
	}


def changed_sides_costs(row_names):
	"""Recipe cost per dish of invoice rows served with changed sides (resto/resto/sides.py): the
	dish's main ingredients plus the sides actually served, at today's costs like recipe_costs."""
	import json

	from resto.resto.costing import get_unit_cost

	if not row_names:
		return {}
	out, memo = {}, {}
	for r in frappe.get_all(
		"Sales Invoice Item",
		filters={"name": ("in", list(row_names)), "resto_sides": ("is", "set")},
		fields=["name", "item_code", "resto_sides"],
	):
		main = frappe.db.get_value("Resto Recipe", {"item": r.item_code, "is_active": 1}, "main_cost")
		if main is None:
			continue
		try:
			served = json.loads(r.resto_sides or "{}")
		except ValueError:
			continue
		out[r.name] = flt(main) + sum(flt(q) * flt(get_unit_cost(item, memo)) for item, q in served.items())
	return out


def excluded_items():
	"""Items that are not food: the delivery fee item."""
	fee = get_settings().delivery_fee_item
	return {fee} if fee else set()


def waste_cost_by_doc(filters):
	"""Waste cost per submitted Resto Waste, from the stock ledger of its Material Issue.

	The ledger is the truth: a back-dated purchase reposts the issue at a new rate, while the
	amounts stored on the waste document stay as they were at submit.
	"""
	rows = frappe.db.sql(
		"""select w.name, sum(-sle.stock_value_difference)
		from `tabResto Waste` w
		join `tabStock Ledger Entry` sle on sle.voucher_type = 'Stock Entry' and sle.voucher_no = w.stock_entry
			and sle.is_cancelled = 0
		where w.docstatus = 1 and w.company = %(company)s and w.posting_date between %(from_date)s and %(to_date)s
		group by w.name""",
		filters,
	)
	return {name: flt(cost) for name, cost in rows}


def waste_cost(filters):
	return sum(waste_cost_by_doc(filters).values())


def prep_adjustments(filters):
	"""Value lost or gained in daily prep entries (normally zero).

	When a prepared ingredient was sold below zero, the sale was costed at its last rate; the
	prep that refills it books the difference here instead of on the sale.
	"""
	return flt(
		frappe.db.sql(
			"""select sum(-sle.stock_value_difference) from `tabStock Ledger Entry` sle
			where sle.voucher_type = 'Stock Entry' and sle.is_cancelled = 0 and sle.company = %(company)s
				and sle.posting_date between %(from_date)s and %(to_date)s
				and sle.voucher_no in (select pi.stock_entry from `tabResto Prep Item` pi
					join `tabResto Prep` p on p.name = pi.parent where p.docstatus = 1 and pi.stock_entry is not null)""",
			filters,
		)[0][0]
	)


def count_differences(filters):
	"""Value of stock count differences (Stock Reconciliation) in the period; negative is a loss.
	Opening stock entries are not count differences and are left out."""
	return flt(
		frappe.db.sql(
			"""select sum(sle.stock_value_difference) from `tabStock Ledger Entry` sle
			join `tabStock Reconciliation` sr on sr.name = sle.voucher_no
			where sle.voucher_type = 'Stock Reconciliation' and sle.is_cancelled = 0 and sle.company = %(company)s
				and sr.purpose != 'Opening Stock'
				and sle.posting_date between %(from_date)s and %(to_date)s""",
			filters,
		)[0][0]
	)


def food_sales(filters):
	"""Net sales of food and drinks (all invoice rows except the delivery fee item)."""
	skip = list(excluded_items()) or [""]
	return flt(
		frappe.db.sql(
			f"""select sum(sii.base_net_amount) from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			where {invoice_condition()} and sii.item_code not in %(skip)s""",
			{**filters, "skip": skip},
		)[0][0]
	)


def other_food_costs(filters):
	"""Waste, prep adjustments and count differences: the costs on top of the cost of food sold."""
	waste = waste_cost(filters)
	prep = prep_adjustments(filters)
	counts = count_differences(filters)
	return frappe._dict(waste=waste, prep=prep, counts=counts, total=waste + prep - counts)


def pct(part, whole):
	return flt(part) / flt(whole) * 100 if flt(whole) else 0


def date_list(filters):
	d, result = getdate(filters.from_date), []
	while d <= getdate(filters.to_date):
		result.append(d)
		d = add_days(d, 1)
	return result
