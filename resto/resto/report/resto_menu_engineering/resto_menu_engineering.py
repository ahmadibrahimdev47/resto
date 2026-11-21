"""Menu engineering (Kasavana & Smith): popularity x contribution margin per dish."""

import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.report_utils import cost_by_invoice_row, excluded_items, prepare_filters, recipe_costs, sold_rows
from resto.resto.utils import get_settings

CLASSES = {
	(True, True): "Star",
	(True, False): "Plowhorse",
	(False, True): "Puzzle",
	(False, False): "Dog",
}
ADVICE = {
	"Star": "Keep quality and place; do not discount",
	"Plowhorse": "Popular but low margin: review recipe cost or price",
	"Puzzle": "Good margin but few sales: promote or reposition",
	"Dog": "Low sales and margin: replace or remove",
}


def execute(filters=None):
	filters = prepare_filters(filters)
	item_group = filters.get("item_group") or get_settings().menu_item_group
	rows = sold_rows(filters, item_group)
	actual = cost_by_invoice_row(filters)
	recipe = recipe_costs()
	# Paid modifiers (extra cheese...) are add-ons, not dishes on the menu.
	skip = excluded_items() | set(
		frappe.get_all("Resto Modifier Option", filters={"item": ("is", "set")}, pluck="item", distinct=True)
	)

	items = {}
	for r in rows:
		if r.item_code in skip:
			continue
		it = items.setdefault(
			r.item_code,
			{"item_code": r.item_code, "item_name": r.item_name, "qty": 0, "net_sales": 0, "cost": 0, "recipe_cost": 0},
		)
		it["qty"] += flt(r.qty)
		it["net_sales"] += flt(r.net_amount)
		it["cost"] += actual.get(r.name, 0)
		it["recipe_cost"] += flt(r.qty) * recipe.get(r.item_code, 0)

	data = [d for d in items.values() if d["qty"] > 0]
	data += unsold_menu_items(item_group, set(items) | skip, recipe)
	n = len(data)
	total_qty = sum(d["qty"] for d in data)
	total_margin = 0
	for d in data:
		if d["qty"]:
			# Stock items without a recipe have an actual cost only; dishes use the ledger cost when it exists.
			d["cost_used"] = d["cost"] if d["cost"] else d["recipe_cost"]
			d["margin"] = d["net_sales"] - d["cost_used"]
			d["price"] = d["net_sales"] / d["qty"]
			d["unit_cost"] = d["cost_used"] / d["qty"]
			d["unit_margin"] = d["margin"] / d["qty"]
		d["food_cost_pct"] = d["unit_cost"] / d["price"] * 100 if d["price"] else 0
		d["mix_pct"] = d["qty"] / total_qty * 100 if total_qty else 0
		total_margin += d["margin"]

	pop_threshold = 70.0 / n if n else 0  # 70% of an equal share
	avg_margin = total_margin / total_qty if total_qty else 0
	for d in data:
		cls = CLASSES[(d["mix_pct"] >= pop_threshold, d["unit_margin"] >= avg_margin)]
		d["class"] = _(cls)
		d["advice"] = _(ADVICE[cls])
	data.sort(key=lambda d: -d["margin"])

	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "qty", "label": _("Qty Sold"), "fieldtype": "Float", "width": 90},
		{"fieldname": "mix_pct", "label": _("Menu Mix %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "price", "label": _("Average Price"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "unit_cost", "label": _("Unit Cost"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "unit_margin", "label": _("Unit Margin"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "food_cost_pct", "label": _("Food Cost %"), "fieldtype": "Percent", "width": 100},
		{"fieldname": "net_sales", "label": _("Net Sales"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "margin", "label": _("Total Margin"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "class", "label": _("Class"), "fieldtype": "Data", "width": 100},
		{"fieldname": "advice", "label": _("Advice"), "fieldtype": "Data", "width": 280},
	]
	counts = {}
	for d in data:
		counts[d["class"]] = counts.get(d["class"], 0) + 1
	chart = {
		"data": {"labels": list(counts), "datasets": [{"name": _("Dishes"), "values": list(counts.values())}]},
		"type": "pie",
	}
	summary = [
		{"label": _("Dishes Sold"), "value": n, "datatype": "Int"},
		{"label": _("Popularity Line (Menu Mix %)"), "value": pop_threshold, "datatype": "Percent"},
		{"label": _("Average Unit Margin"), "value": avg_margin, "datatype": "Currency"},
		{"label": _("Total Margin"), "value": total_margin, "datatype": "Currency", "indicator": "Green"},
	]
	return columns, data, None, chart, summary


def unsold_menu_items(item_group, seen, recipe):
	"""Priced menu items that sold nothing in the period: they are on the menu, so they count."""
	settings = get_settings()
	if not item_group or not settings.selling_price_list:
		return []
	lft, rgt = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"])
	rows = frappe.db.sql(
		"""select i.name, i.item_name, max(ip.price_list_rate) as price
		from `tabItem` i
		join `tabItem Group` g on g.name = i.item_group
		join `tabItem Price` ip on ip.item_code = i.name and ip.price_list = %s
		where g.lft >= %s and g.rgt <= %s and i.disabled = 0 and i.is_sales_item = 1
		group by i.name""",
		(settings.selling_price_list, lft, rgt),
		as_dict=True,
	)
	result = []
	for r in rows:
		if r.name in seen:
			continue
		cost = recipe.get(r.name, 0)
		result.append(
			{"item_code": r.name, "item_name": r.item_name, "qty": 0, "net_sales": 0, "cost": 0, "recipe_cost": 0,
			 "cost_used": 0, "margin": 0, "price": flt(r.price), "unit_cost": cost, "unit_margin": flt(r.price) - cost}
		)
	return result
