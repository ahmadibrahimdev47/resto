"""Packaging and automatic consumables (D-217, D-224).

A dish's recipe lists the boxes, cups and lids it takes (Packaging), each row ticked for the order
types that take it: takeaway and delivery by default, dine-in when a dine-in dish is served in it
too. Resto Settings lists the Automatic Consumables: what every order of the ticked types takes,
per order (a bag, a receipt roll) or per guest (napkins, a wet wipe, cutlery). The cashier may add
more of the rows marked for it to a big order.

They are billed as their own invoice lines, at no price, of the stock items themselves (Sales
Invoice Item.resto_packaging: "Dish" or "Order"). ERPNext then posts them as it posts any stock line:
out of the POS Profile's warehouse (the kitchen, which the Setup Check holds it to) at the valuation
rate, into that invoice's cost of goods sold. The dish's own bundle is never touched, so a dish used
inside another dish never brings its packaging. A return drops these lines: what was used is never
put back in stock.

The order type is read when the bill is made, so an order changed between dine-in and takeaway
before it is paid takes the consumables of what it is when it is paid. A bill split in parts takes,
for the dishes, what the order's billed dishes take in all after this part, in whole units where the
item counts in whole units (rounded up: half a box is a box), less what the order's other bills not
cancelled already took: the parts always add up to what the whole order takes, a cancelled part
included. The per-order and per-guest rows go on the first bill of the order that is not cancelled
(like the delivery fee).
"""

import json
import math
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.utils import get_settings

TYPE_FLAG = {"Dine In": "dine_in", "Takeaway": "takeaway", "Delivery": "delivery"}
DISH, ORDER = "Dish", "Order"
PER_GUEST = "Per Guest"
EPS = 1e-9
MAX_EXTRA = 50


def flag(order_type):
	return TYPE_FLAG.get(order_type)


def _whole(item_code):
	uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	return bool(uom and cint(frappe.get_cached_value("UOM", uom, "must_be_whole_number")))


def _precision():
	"""The invoice line's quantity precision: what ERPNext stores, so what was taken is compared exactly."""
	return frappe.get_precision("Sales Invoice Item", "qty") or 3


def _units(item_code, qty):
	"""A quantity as it is taken: whole units (rounded up) for an item counted in whole units, else
	at the invoice's quantity precision."""
	if _whole(item_code):
		return math.ceil(flt(qty) - 1e-6)
	return flt(qty, _precision())


def per_dish(item_code, order_type):
	"""{stock item: stock qty} one dish takes on an order of this type."""
	from resto.resto.doctype.resto_recipe.resto_recipe import MENU, aggregate, get_active_recipe

	field = flag(order_type)
	name = field and get_active_recipe(item_code, MENU)
	if not name:
		return {}
	rows = frappe.get_cached_doc("Resto Recipe", name).get("packaging") or []
	return aggregate([r for r in rows if cint(r.get(field))])


def consumables():
	return get_settings().get("consumables") or []


def addable(order_type=None):
	"""The consumables the cashier may add more of: [{item_code, item_name, dine_in, takeaway, delivery}]."""
	field = flag(order_type) if order_type else None
	return [
		{
			"item_code": r.item_code,
			"item_name": r.item_name or r.item_code,
			**{f: cint(r.get(f)) for f in TYPE_FLAG.values()},
		}
		for r in consumables()
		if cint(r.can_add_more) and (not order_type or (field and cint(r.get(field))))
	]


def extras(order):
	"""{item: qty} the cashier added to this order, as stored."""
	try:
		data = json.loads(order.get("extra_packaging") or "{}")
	except ValueError:
		return {}
	if not isinstance(data, dict):
		return {}
	return {k: flt(v) for k, v in data.items() if flt(v) > EPS}


def guests(order):
	return max(cint(order.get("guests")), 1)


def per_order(order):
	"""{item: qty} the order takes once: the Automatic Consumables of its type (per guest times its
	guests) plus what the cashier added of the rows that may be added to it."""
	field = flag(order.order_type)
	out = defaultdict(float)
	if not field:
		return {}
	for row in consumables():
		if cint(row.get(field)):
			out[row.item_code] += flt(row.qty) * (guests(order) if row.basis == PER_GUEST else 1)
	allowed = {r["item_code"] for r in addable(order.order_type)}
	for item, qty in extras(order).items():
		if item in allowed:
			out[item] += min(cint(qty), MAX_EXTRA)
	return {k: v for k, v in out.items() if v > EPS}


def _billed(active_invoices, kind):
	"""{item: qty} of this kind of line on the order's bills not cancelled."""
	if not active_invoices:
		return {}
	return {
		r.item_code: flt(r.qty)
		for r in frappe.db.sql(
			"""select sii.item_code, sum(sii.stock_qty) qty from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			where sii.parent in %s and sii.resto_packaging = %s and si.docstatus < 2
			group by sii.item_code""",
			(list(active_invoices), kind),
			as_dict=True,
		)
	}


def _order_rows_billed(active_invoices):
	return bool(
		active_invoices
		and frappe.db.exists("Sales Invoice Item", {"parent": ("in", active_invoices), "resto_packaging": ORDER})
	)


def lines(order, to_bill, active_invoices):
	"""[(item, qty, kind)]: the packaging and consumable lines of this bill of the order."""
	if not flag(order.order_type):
		return []
	billing = {row.name: flt(qty) for row, qty in to_bill}
	need = defaultdict(float)
	for row in order.items:
		total = flt(row.billed_qty) + billing.get(row.name, 0)
		if total <= EPS:
			continue
		for item, qty in per_dish(row.item_code, order.order_type).items():
			need[item] += qty * total
	taken = _billed(active_invoices, DISH)
	out = []
	for item in need:
		qty = flt(_units(item, need[item]) - flt(taken.get(item)), _precision())
		if qty > 0:
			out.append((item, qty, DISH))
	if not _order_rows_billed(active_invoices):
		for item, qty in per_order(order).items():
			qty = _units(item, qty)
			if qty > 0:
				out.append((item, qty, ORDER))
	if out:
		check_items({item for item, _q, _k in out})
	return out


def check_items(items):
	"""Every packaging or consumable item can be billed at no price and taken from stock."""
	if cint(frappe.db.get_single_value("Selling Settings", "validate_selling_price")):
		# ERPNext would refuse a line sold below its valuation rate; these lines have no price.
		frappe.throw(
			_("Packaging and consumables are billed at no price, but Selling Settings checks the selling price against the valuation rate. Untick Validate Selling Price for Item Against Purchase Rate or Valuation Rate in Selling Settings.")
		)
	for code in items:
		problem = item_problem(code)
		if problem:
			frappe.throw(problem)


def item_problem(code):
	"""Why this item cannot be a packaging or consumable line, or None."""
	item = frappe.get_cached_value(
		"Item", code, ["item_name", "disabled", "is_stock_item", "is_sales_item", "has_variants", "has_batch_no", "has_serial_no"], as_dict=True
	)
	if not item:
		return _("Packaging or consumable {0} no longer exists").format(frappe.bold(code))
	name = frappe.bold(item.item_name or code)
	if item.disabled or item.has_variants:
		return _("Packaging or consumable {0} is disabled or is a template").format(name)
	if not item.is_stock_item:
		return _("Packaging or consumable {0} is not a stock item, so it would not be taken from stock").format(name)
	if not item.is_sales_item:
		return _("Packaging or consumable {0} is billed at no price on the bill: tick Allow Sales on the item").format(name)
	if item.has_batch_no or item.has_serial_no:
		return _("Packaging or consumable {0} has batch or serial numbers, which the bill cannot choose").format(name)
	return None


def check_stock(si, throw=True):
	"""A packaging or consumable item short in the warehouse, named: before the bill is saved (throw),
	and on the bill shown before the payment is taken (the message, or None)."""
	need = defaultdict(float)
	for d in si.items:
		if d.get("resto_packaging"):
			need[(d.item_code, d.warehouse)] += flt(d.stock_qty or d.qty)
	if not need or cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock")):
		return None
	for (item, warehouse), qty in need.items():
		if cint(frappe.get_cached_value("Item", item, "allow_negative_stock")):
			continue
		have = flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"))
		if have + EPS < qty:
			message = _("Not enough {0} in {1} for this bill: {2} needed, {3} in stock. Receive it into stock first; nothing was billed.").format(
				frappe.bold(frappe.get_cached_value("Item", item, "item_name") or item),
				frappe.bold(warehouse),
				frappe.format(qty, "Float"),
				frappe.format(have, "Float"),
			)
			if throw:
				frappe.throw(message)
			return message
	return None


def set_extra(doc, item_code, qty):
	"""The cashier's extra of one of the consumables that may be added (a second bag). 0 clears it."""
	qty = cint(qty)
	if qty < 0 or qty > MAX_EXTRA:
		frappe.throw(_("Extra packaging is 0 to {0}").format(MAX_EXTRA))
	if _order_rows_billed([d.sales_invoice for d in doc.invoices if not d.cancelled]):
		frappe.throw(_("The order's packaging is already billed"))
	if qty and item_code not in {r["item_code"] for r in addable(doc.order_type)}:
		frappe.throw(_("{0} cannot be added to this order in Resto Settings").format(frappe.bold(item_code)))
	data = extras(doc)
	if qty:
		data[item_code] = qty
	else:
		data.pop(item_code, None)
	doc.extra_packaging = json.dumps(data, sort_keys=True) if data else None


def drop_from_return(doc, method=None):
	"""Sales Invoice before validate: a return never takes packaging or consumables back into stock
	(they were used). A row is one when it is flagged, when it returns a flagged row of the original
	bill, or, when it names no original row, when its item is only packaging on the original bill."""
	if doc.doctype != "Sales Invoice" or not cint(doc.get("is_return")):
		return
	flagged_rows, only_packaging = set(), set()
	if doc.get("return_against"):
		rows = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": doc.return_against, "parenttype": "Sales Invoice"},
			fields=["name", "item_code", "resto_packaging"],
		)
		flagged_rows = {r.name for r in rows if r.resto_packaging}
		only_packaging = {r.item_code for r in rows if r.resto_packaging} - {r.item_code for r in rows if not r.resto_packaging}

	def used(d):
		if d.get("resto_packaging"):
			return True
		if d.get("sales_invoice_item"):
			return d.sales_invoice_item in flagged_rows
		return d.item_code in only_packaging

	keep = [d for d in doc.items if not used(d)]
	if len(keep) == len(doc.items):
		return
	if not keep:
		frappe.throw(_("Packaging and consumables used for an order are not returned to stock: return its dishes instead"))
	doc.set("items", keep)
	for i, d in enumerate(doc.items, 1):
		d.idx = i
