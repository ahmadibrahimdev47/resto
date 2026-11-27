"""The items page (D-288): each item at a glance, with its picture, group, unit, selling price, what it
is used for and whether it is active. It reads through the user's own Item rights and writes nothing:
an item opens in its form."""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate
from frappe.utils.nestedset import get_descendants_of

from resto.resto.utils import get_settings

PAGE_LENGTH = 40
FIELDS = (
	"name",
	"item_name",
	"item_group",
	"stock_uom",
	"image",
	"is_sales_item",
	"is_purchase_item",
	"is_stock_item",
	"default_bom",
	"disabled",
	"has_variants",
	"variant_of",
)
# The tag filters: a flag on the item, or an item made in the kitchen by its BOM.
TAGS = {"sales": "is_sales_item", "purchase": "is_purchase_item", "stock": "is_stock_item"}


def _check():
	if not frappe.has_permission("Item", "read"):
		frappe.throw(_("You are not allowed to see the items"), frappe.PermissionError)


def price_list():
	"""The selling price list the floor bills with (Resto Settings, its POS Profile), else the
	site's default selling price list."""
	settings = get_settings()
	return (
		settings.get("selling_price_list")
		or (frappe.db.get_value("POS Profile", settings.pos_profile, "selling_price_list") if settings.get("pos_profile") else None)
		or frappe.db.get_single_value("Selling Settings", "selling_price_list")
	)


def selling_prices(item_codes, price_list_name, day=None):
	"""{item: {rate, uom}}: each item's price valid on day in the price list, not a customer's own
	(the latest one to start, as the floor bills)."""
	if not item_codes or not price_list_name:
		return {}
	day = getdate(day)
	prices = {}
	for p in frappe.get_all(
		"Item Price",
		filters={"price_list": price_list_name, "selling": 1, "item_code": ("in", list(item_codes))},
		fields=["item_code", "price_list_rate", "uom", "customer", "valid_from", "valid_upto"],
		order_by="valid_from asc",
	):
		if p.customer or (p.valid_from and getdate(p.valid_from) > day) or (p.valid_upto and getdate(p.valid_upto) < day):
			continue
		prices[p.item_code] = frappe._dict(rate=flt(p.price_list_rate), uom=p.uom)
	return prices


@frappe.whitelist()
def get_items(search=None, item_group=None, tag=None, status=None, start=0, page_length=PAGE_LENGTH):
	"""A page of items for the items page: the user's own Item rights decide which."""
	_check()
	start = max(cint(start), 0)
	page_length = min(max(cint(page_length) or PAGE_LENGTH, 1), 100)
	filters = []
	if item_group:
		filters.append(["item_group", "in", [item_group, *get_descendants_of("Item Group", item_group)]])
	if tag in TAGS:
		filters.append([TAGS[tag], "=", 1])
	elif tag == "prep":
		filters.append(["default_bom", "is", "set"])
	if status in ("active", "disabled"):
		filters.append(["disabled", "=", 1 if status == "disabled" else 0])
	or_filters = None
	search = (search or "").strip()
	if search:
		like = f"%{search}%"
		or_filters = [["item_name", "like", like], ["name", "like", like]]
	rows = frappe.get_list(
		"Item",
		filters=filters,
		or_filters=or_filters,
		fields=list(FIELDS),
		order_by="disabled asc, item_name asc",
		start=start,
		page_length=page_length + 1,
	)
	more = len(rows) > page_length
	rows = rows[:page_length]
	plist = price_list()
	prices = selling_prices([r.name for r in rows], plist)
	for r in rows:
		price = prices.get(r.name)
		r.price = price.rate if price else None
		# A price set for another unit than the stock unit says which.
		r.price_uom = price.uom if price and price.uom and price.uom != r.stock_uom else None
	return {
		"items": rows,
		"more": more,
		"price_list": plist,
		"currency": frappe.db.get_value("Price List", plist, "currency") if plist else None,
		"can_write": frappe.has_permission("Item", "write"),
		"can_create": frappe.has_permission("Item", "create"),
	}


@frappe.whitelist()
def get_groups():
	"""The item groups the user's items are in, for the group filter."""
	_check()
	return [r.item_group for r in frappe.get_list("Item", fields=["item_group"], group_by="item_group", order_by="item_group asc") if r.item_group]
