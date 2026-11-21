"""Purchase price changes of ingredients and the recipes they hit.

Prices come from the stock ledger (incoming rate per stock unit) of purchase receipts and
purchase invoices that update stock: the last price in the period against the price before it.
"""

import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.report_utils import prepare_filters


def execute(filters=None):
	filters = prepare_filters(filters)
	min_change = flt(filters.get("min_change"))
	rows = frappe.db.sql(
		"""
		select sle.item_code, sle.posting_date, sle.posting_time, sle.creation, sle.incoming_rate, sle.voucher_type, sle.voucher_no
		from `tabStock Ledger Entry` sle
		where sle.is_cancelled = 0 and sle.company = %(company)s and sle.actual_qty > 0
			and sle.voucher_type in ('Purchase Receipt', 'Purchase Invoice') and sle.posting_date <= %(to_date)s
		order by sle.item_code, sle.posting_date, sle.posting_time, sle.creation
		""",
		filters,
		as_dict=True,
	)
	by_item = {}
	for r in rows:
		by_item.setdefault(r.item_code, []).append(r)

	usage = {}
	for r in frappe.db.sql(
		"""select ri.item_code, count(distinct r.name) as recipes
		from `tabResto Recipe Item` ri join `tabResto Recipe` r on r.name = ri.parent
		where r.is_active = 1 group by ri.item_code""",
		as_dict=True,
	):
		usage[r.item_code] = r.recipes

	data = []
	for item_code, entries in by_item.items():
		in_period = [e for e in entries if e.posting_date >= filters.from_date]
		if not in_period:
			continue
		last = in_period[-1]
		before = [e for e in entries if (e.posting_date, e.posting_time, e.creation) < (last.posting_date, last.posting_time, last.creation)]
		previous = before[-1] if before else None
		if not previous or not flt(previous.incoming_rate):
			continue
		change = (flt(last.incoming_rate) - flt(previous.incoming_rate)) / flt(previous.incoming_rate) * 100
		if abs(change) < min_change:
			continue
		data.append(
			{
				"item_code": item_code,
				"item_name": frappe.get_cached_value("Item", item_code, "item_name"),
				"uom": frappe.get_cached_value("Item", item_code, "stock_uom"),
				"previous_rate": flt(previous.incoming_rate),
				"previous_date": previous.posting_date,
				"last_rate": flt(last.incoming_rate),
				"last_date": last.posting_date,
				"change": change,
				"recipes": usage.get(item_code, 0),
				"voucher_type": last.voucher_type,
				"voucher_no": last.voucher_no,
			}
		)
	data.sort(key=lambda d: -abs(d["change"]))
	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 130},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 160},
		{"fieldname": "uom", "label": _("UOM"), "fieldtype": "Data", "width": 60},
		{"fieldname": "previous_rate", "label": _("Previous Price"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "previous_date", "label": _("Previous Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "last_rate", "label": _("Last Price"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "last_date", "label": _("Last Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "change", "label": _("Change %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "recipes", "label": _("Recipes Using It"), "fieldtype": "Int", "width": 110},
		{"fieldname": "voucher_type", "label": _("Voucher Type"), "fieldtype": "Data", "hidden": 1},
		{"fieldname": "voucher_no", "label": _("Last Voucher"), "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 140},
	]
	return columns, data, _("After a price change, press Update All Recipe Costs in Resto Settings to refresh recipe costs."), None
