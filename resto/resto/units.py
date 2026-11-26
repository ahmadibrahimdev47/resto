"""Quantities typed in a recipe unit, kept in the stock unit (D-250).

The kitchen thinks in grams and millilitres: 20 g of mozzarella, not 0.02 Kg. Where a quantity is
typed by hand (a staff meal's free ingredient, a waste row), it is typed in the unit a recipe would
use, Gram for an item stocked by the Kg, Millilitre for one stocked by the Litre, or any unit the
item converts to, and turned into the stock unit here, with the item's own conversion (or the
site's), the same way Resto Recipe does. The stock line and the stock issue stay in the stock unit.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.utils import CASHIER, KITCHEN, require_role

# The recipe unit of a stock unit (the same as resto.RECIPE_UNITS on the desk).
RECIPE_UNITS = {"Kg": "Gram", "Litre": "Millilitre"}


def _precision():
	return frappe.get_precision("Stock Entry Detail", "qty") or 3


def conversion(item_code, uom):
	"""How many stock units one `uom` of the item is: the item's own Units of Measure row (or its
	template's), else the site's UOM Conversion Factor; 0 when neither has it. ERPNext's
	get_conversion_factor answers 1 for a unit it does not know, which would take 5 Gram of an item
	counted in Nos as 5 Nos, so it is not used here."""
	stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	if not uom or uom == stock_uom:
		return 1.0
	parents = [item_code]
	variant_of = frappe.get_cached_value("Item", item_code, "variant_of")
	if variant_of:
		parents.append(variant_of)
	for parent in parents:
		value = flt(frappe.db.get_value("UOM Conversion Detail", {"parent": parent, "parenttype": "Item", "uom": uom}, "conversion_factor"))
		if value > 0:
			return value
	try:
		from erpnext.stock.doctype.item.item import get_uom_conv_factor
	except ImportError:
		get_uom_conv_factor = _site_factor
	return max(flt(get_uom_conv_factor(uom, stock_uom)), 0.0)


def _site_factor(uom, stock_uom):
	"""The site's UOM Conversion Factor between the two units, either way round, or 0."""
	value = flt(frappe.db.get_value("UOM Conversion Factor", {"from_uom": uom, "to_uom": stock_uom}, "value"))
	if value > 0:
		return value
	back = flt(frappe.db.get_value("UOM Conversion Factor", {"from_uom": stock_uom, "to_uom": uom}, "value"))
	return 1 / back if back > 0 else 0


def factor(item_code, uom):
	"""How many stock units one `uom` of the item is; refused when the item has no conversion."""
	value = conversion(item_code, uom)
	if value <= 0:
		stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
		frappe.throw(
			_("{0} has no conversion from {1} to its stock unit {2}: add it on the item's Units of Measure").format(
				frappe.bold(frappe.get_cached_value("Item", item_code, "item_name") or item_code), _(uom), _(stock_uom)
			)
		)
	return value


def to_stock(item_code, qty, uom):
	"""`qty` of `uom` in the item's stock unit, to the stock precision."""
	return flt(flt(qty) * factor(item_code, uom), _precision())


def units_of(item_code):
	"""The units a quantity of the item can be typed in, the recipe unit first when it converts."""
	stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	units = {stock_uom: 1.0}
	recipe = RECIPE_UNITS.get(stock_uom)
	if recipe:
		value = conversion(item_code, recipe)
		if value > 0:
			units[recipe] = value
	for row in frappe.get_all(
		"UOM Conversion Detail", filters={"parent": item_code, "parenttype": "Item"}, fields=["uom", "conversion_factor"]
	):
		if row.uom and flt(row.conversion_factor) > 0:
			units.setdefault(row.uom, flt(row.conversion_factor))
	default = recipe if recipe in units else stock_uom
	return {
		"stock_uom": stock_uom,
		"default": default,
		"units": [{"uom": u, "factor": f} for u, f in sorted(units.items(), key=lambda x: (x[0] != default, x[0] != stock_uom, x[0]))],
		"precision": _precision(),
	}


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def uom_query(doctype, txt, searchfield, start, page_len, filters):
	"""The units a row's quantity can be typed in, for the Unit picker of a waste row: only those the
	item truly converts to, so Gram is never offered on an item counted in Nos."""
	require_role(CASHIER, KITCHEN)
	item_code = (filters or {}).get("item_code")
	if not item_code or not frappe.db.exists("Item", item_code):
		return []
	txt = (txt or "").lower()
	names = [u["uom"] for u in units_of(item_code)["units"] if txt in u["uom"].lower() or txt in _(u["uom"]).lower()]
	return [(n,) for n in names[cint(start) : cint(start) + cint(page_len)]]


@frappe.whitelist()
def item_units(item_code):
	"""What the free ingredient and waste dialogs offer for the item's quantity."""
	require_role(CASHIER, KITCHEN)
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found").format(item_code))
	return units_of(item_code)
