"""Food cost: what the recipes say the sold food should cost (theoretical) against what the
stock ledger actually posted when it was sold, plus waste and count differences."""

import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.report_utils import (
	cost_by_invoice_row,
	excluded_items,
	other_food_costs,
	pct,
	prepare_filters,
	recipe_costs,
	sold_rows,
)


def execute(filters=None):
	filters = prepare_filters(filters)
	group_by = filters.get("group_by") or "Item Group"
	rows = sold_rows(filters, filters.get("item_group"))
	actual = cost_by_invoice_row(filters)
	recipe = recipe_costs()
	skip = excluded_items()

	groups = {}
	for r in rows:
		if r.item_code in skip:
			continue
		key = {"Item": r.item_code, "Item Group": r.item_group, "Day": str(r.posting_date)}[group_by]
		g = groups.setdefault(
			key,
			{"group": key, "item_name": r.item_name if group_by == "Item" else "", "qty": 0, "net_sales": 0,
			 "actual_cost": 0, "theoretical_cost": 0, "no_recipe": 0},
		)
		g["qty"] += flt(r.qty)
		g["net_sales"] += flt(r.net_amount)
		g["actual_cost"] += actual.get(r.name, 0)
		if r.item_code in recipe:
			g["theoretical_cost"] += flt(r.qty) * recipe[r.item_code]
		else:
			# Items sold without a recipe (bottled drinks...) count at their actual cost.
			g["theoretical_cost"] += actual.get(r.name, 0)
			g["no_recipe"] += 1

	data = sorted(groups.values(), key=lambda d: d["group"] if group_by == "Day" else -d["net_sales"])
	for d in data:
		d["actual_pct"] = pct(d["actual_cost"], d["net_sales"])
		d["theoretical_pct"] = pct(d["theoretical_cost"], d["net_sales"])
		d["variance"] = d["actual_cost"] - d["theoretical_cost"]

	sales = sum(d["net_sales"] for d in data)
	cogs = sum(d["actual_cost"] for d in data)
	theo = sum(d["theoretical_cost"] for d in data)
	# Waste, prep adjustments and count differences belong to the whole kitchen, not to one item group.
	extra = other_food_costs(filters) if not filters.get("item_group") else frappe._dict(waste=0, prep=0, counts=0, total=0)
	waste, counts = extra.waste, extra.counts
	total_cost = cogs + extra.total  # a negative count difference is a loss, adding to cost

	columns = [{"fieldname": "group", "label": _(group_by), "fieldtype": "Link" if group_by != "Day" else "Date",
	            "options": group_by if group_by != "Day" else None, "width": 160}]
	if group_by == "Item":
		columns.append({"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170})
	columns += [
		{"fieldname": "qty", "label": _("Qty Sold"), "fieldtype": "Float", "width": 90},
		{"fieldname": "net_sales", "label": _("Net Sales"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "theoretical_cost", "label": _("Recipe Cost"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "theoretical_pct", "label": _("Recipe Food Cost %"), "fieldtype": "Percent", "width": 110},
		{"fieldname": "actual_cost", "label": _("Cost Posted"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "actual_pct", "label": _("Actual Food Cost %"), "fieldtype": "Percent", "width": 110},
		{"fieldname": "variance", "label": _("Variance"), "fieldtype": "Currency", "width": 110},
	]
	summary = [
		{"label": _("Net Sales"), "value": sales, "datatype": "Currency", "indicator": "Blue"},
		{"label": _("Recipe Food Cost %"), "value": pct(theo, sales), "datatype": "Percent"},
		{"label": _("Cost of Food Sold"), "value": cogs, "datatype": "Currency"},
		{"label": _("Waste"), "value": waste, "datatype": "Currency", "indicator": "Orange"},
		{"label": _("Prep Adjustments"), "value": extra.prep, "datatype": "Currency"},
		{"label": _("Stock Count Differences"), "value": counts, "datatype": "Currency",
		 "indicator": "Red" if counts < 0 else "Green"},
		{"label": _("Total Food Cost %"), "value": pct(total_cost, sales), "datatype": "Percent",
		 "indicator": "Red" if pct(total_cost, sales) > flt(frappe.db.get_single_value("Resto Settings", "food_cost_alert_percent") or 100) else "Green"},
	]
	chart = {
		"data": {
			"labels": [str(d["group"]) for d in data][:30],
			"datasets": [
				{"name": _("Recipe Food Cost %"), "values": [flt(d["theoretical_pct"], 1) for d in data][:30]},
				{"name": _("Actual Food Cost %"), "values": [flt(d["actual_pct"], 1) for d in data][:30]},
			],
		},
		"type": "bar",
	}
	return columns, data, _(
		"Recipe cost uses today's recipe costs. Cost Posted is what the stock ledger booked at the time of sale. "
		"Total Food Cost adds waste, prep adjustments and stock count losses (not when filtered by item group)."
	), chart, summary
