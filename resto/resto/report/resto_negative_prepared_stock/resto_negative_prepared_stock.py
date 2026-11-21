"""Prepared ingredients sold below zero: the kitchen sold more than the prep that was posted."""

import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.utils import get_settings


def execute(filters=None):
	filters = frappe._dict(filters or {})
	settings = get_settings()
	warehouse = filters.get("warehouse") or settings.kitchen_warehouse
	company = filters.get("company") or settings.company
	data = frappe.db.sql(
		"""
		select b.item_code, i.item_name, b.warehouse, b.actual_qty, i.stock_uom as uom, b.valuation_rate,
			r.name as recipe, r.par_qty, r.cost_per_unit
		from `tabBin` b
		join `tabItem` i on i.name = b.item_code
		join `tabResto Recipe` r on r.item = b.item_code and r.recipe_type = 'Prepared Ingredient'
		join `tabWarehouse` w on w.name = b.warehouse
		where b.actual_qty < 0 and w.company = %(company)s and (%(warehouse)s = '' or b.warehouse = %(warehouse)s)
		order by b.actual_qty
		""",
		{"company": company, "warehouse": warehouse or ""},
		as_dict=True,
	)
	for d in data:
		d.missing_value = -flt(d.actual_qty) * flt(d.cost_per_unit)
	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "warehouse", "label": _("Warehouse"), "fieldtype": "Link", "options": "Warehouse", "width": 140},
		{"fieldname": "actual_qty", "label": _("Qty"), "fieldtype": "Float", "width": 90},
		{"fieldname": "uom", "label": _("UOM"), "fieldtype": "Data", "width": 70},
		{"fieldname": "par_qty", "label": _("Par Level"), "fieldtype": "Float", "width": 90},
		{"fieldname": "missing_value", "label": _("Value at Recipe Cost"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "recipe", "label": _("Recipe"), "fieldtype": "Link", "options": "Resto Recipe", "width": 140},
	]
	return columns, data, _("Post a Daily Prep for these items so the stock and its cost become right."), None
