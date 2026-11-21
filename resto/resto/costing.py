"""Recipe costing helpers.

Unit cost of an item, per stock unit:
- a prepared ingredient with an active recipe: its recipe cost divided by the yield (live, recursive);
- a menu dish with an active recipe: the cost of its main ingredients (sides excluded);
- any other item: valuation rate in the kitchen warehouse, then the main store, then any
  warehouse, then the item's valuation rate, then its last purchase rate.
"""

import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.doctype.resto_settings.resto_settings import get_settings

PREPARED = "Prepared Ingredient"
MENU = "Menu Dish"


def get_unit_cost(item_code, memo=None, stack=()):
	memo = {} if memo is None else memo
	if item_code in memo:
		return memo[item_code]
	if item_code in stack:
		frappe.throw(
			_("Recipe loop: {0}").format(" → ".join([*stack, item_code])), title=_("Recipe Loop")
		)

	recipe = frappe.db.get_value(
		"Resto Recipe",
		{"item": item_code, "is_active": 1},
		["name", "recipe_type", "quantity"],
		as_dict=True,
	)
	if recipe:
		rows = frappe.get_all(
			"Resto Recipe Item",
			filters={"parenttype": "Resto Recipe", "parent": recipe.name, "parentfield": "items"},
			fields=["item_code", "stock_qty"],
		)
		total = sum(
			get_unit_cost(r.item_code, memo, (*stack, item_code)) * flt(r.stock_qty) for r in rows
		)
		if recipe.recipe_type == PREPARED:
			cost = total / flt(recipe.quantity or 1)
		else:
			cost = total
	else:
		cost = get_stock_rate(item_code)

	memo[item_code] = flt(cost)
	return memo[item_code]


def get_stock_rate(item_code):
	settings = get_settings()
	for warehouse in (settings.kitchen_warehouse, settings.main_warehouse):
		if not warehouse:
			continue
		rate = frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"
		)
		if flt(rate) > 0:
			return flt(rate)

	bins = frappe.get_all(
		"Bin",
		filters={"item_code": item_code, "valuation_rate": [">", 0]},
		fields=["valuation_rate"],
		order_by="modified desc",
		limit=1,
	)
	if bins:
		return flt(bins[0].valuation_rate)

	valuation_rate, last_purchase_rate = frappe.db.get_value(
		"Item", item_code, ["valuation_rate", "last_purchase_rate"]
	) or (0, 0)
	return flt(valuation_rate) or flt(last_purchase_rate)


def get_selling_rate(item_code):
	"""The price the floor charges today, the same lookup as orders use."""
	from resto.resto.doctype.resto_order.resto_order import get_item_rate

	price_list = get_settings().selling_price_list
	return get_item_rate(item_code, price_list, throw=False) if price_list else 0
