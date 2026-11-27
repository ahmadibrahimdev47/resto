"""The items page (D-288): each item at a glance, with its picture, group, unit, selling price, what it
is used for and whether it is active. It reads through the user's own Item rights: an item opens in
its form. The one thing it writes is an item's reorder level per warehouse (D-290)."""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now
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
# Item Reorder's material request types; ERPNext's daily reorder job raises one of these.
REORDER_TYPES = ("Purchase", "Transfer", "Material Issue", "Manufacture")
REORDER_FIELDS = ("warehouse", "warehouse_reorder_level", "warehouse_reorder_qty", "material_request_type")


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
	kitchen = get_settings().get("kitchen_warehouse")
	levels = reorder_levels([r.name for r in rows])
	for r in rows:
		mine = levels.get(r.name, [])
		# The kitchen's level shows on the card; an item reordered only for another store shows that one.
		r.reorder = next((l for l in mine if l.warehouse == kitchen), None) or (mine[0] if mine else None)
		r.reorder_count = len(mine)
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
		"can_reorder": can_set_reorder(),
		"kitchen_warehouse": kitchen,
		"auto_reorder": cint(frappe.db.get_single_value("Stock Settings", "auto_indent")),
	}


@frappe.whitelist()
def get_groups():
	"""The item groups the user's items are in, for the group filter."""
	_check()
	return [r.item_group for r in frappe.get_list("Item", fields=["item_group"], group_by="item_group", order_by="item_group asc") if r.item_group]


# ---------------------------------------------------------------------------------- reorder levels (D-290)


def can_set_reorder():
	"""The restaurant manager sets reorder levels without the right to change the item; so does
	whoever may change items anyway."""
	return "Resto Manager" in frappe.get_roles() or bool(frappe.has_permission("Item", "write"))


def reorder_levels(item_codes):
	"""{item: [{warehouse, level, qty, type}]}: each item's reorder rows."""
	if not item_codes:
		return {}
	out = {}
	for r in frappe.get_all(
		"Item Reorder",
		filters={"parenttype": "Item", "parentfield": "reorder_levels", "parent": ("in", list(item_codes))},
		fields=["parent", *REORDER_FIELDS],
		order_by="idx asc",
	):
		out.setdefault(r.parent, []).append(
			frappe._dict(
				warehouse=r.warehouse,
				level=flt(r.warehouse_reorder_level),
				qty=flt(r.warehouse_reorder_qty),
				type=r.material_request_type,
			)
		)
	return out


@frappe.whitelist()
def get_reorder(item_code):
	_check()
	if not frappe.has_permission("Item", "read", item_code):
		frappe.throw(_("You are not allowed to see this item"), frappe.PermissionError)
	return reorder_levels([item_code]).get(item_code, [])


@frappe.whitelist(methods=["POST"])
def set_reorder(item_code, warehouse, level=0, qty=0, material_request_type="Purchase", remove=0):
	"""Set (or with remove, clear) an item's reorder level for one warehouse: only its Item Reorder
	row is written, nothing else on the item. ERPNext's daily reorder job reads these rows."""
	if not can_set_reorder():
		frappe.throw(_("Only the restaurant manager can set reorder levels"), frappe.PermissionError)
	item = frappe.db.get_value("Item", item_code, ["name", "is_stock_item", "has_variants", "stock_uom"], as_dict=True)
	if not item:
		frappe.throw(_("Item {0} not found").format(item_code), frappe.DoesNotExistError)
	if not item.is_stock_item or item.has_variants:
		frappe.throw(_("Only a stock item has a reorder level"))
	wh = frappe.db.get_value("Warehouse", warehouse, ["name", "is_group", "disabled"], as_dict=True)
	if not wh or wh.is_group or wh.disabled:
		frappe.throw(_("Choose a warehouse that holds stock"))
	where = {"parenttype": "Item", "parentfield": "reorder_levels", "parent": item_code, "warehouse": warehouse}
	existing = frappe.db.get_value("Item Reorder", where, "name")

	if cint(remove):
		if existing:
			frappe.db.delete("Item Reorder", {"name": existing})
			_touched(item_code, _("Reorder level for {0} removed").format(warehouse))
		return None

	level, qty = flt(level), flt(qty)
	if level < 0:
		frappe.throw(_("The reorder level cannot be below zero"))
	if qty <= 0:
		frappe.throw(_("The quantity to order must be more than zero"))
	if material_request_type not in REORDER_TYPES:
		frappe.throw(_("Choose what the reorder raises"))
	values = {"warehouse_reorder_level": level, "warehouse_reorder_qty": qty, "material_request_type": material_request_type}
	if existing:
		frappe.db.set_value("Item Reorder", existing, values)
	else:
		idx = cint(
			frappe.db.sql(
				"select max(idx) from `tabItem Reorder` where parenttype = 'Item' and parentfield = 'reorder_levels' and parent = %s",
				item_code,
			)[0][0]
		)
		frappe.get_doc({"doctype": "Item Reorder", **where, "idx": idx + 1, **values}).db_insert()
	_touched(
		item_code,
		_("Reorder level for {0} set to {1} {2}, order {3}").format(warehouse, _num(level), _(item.stock_uom or ""), _num(qty)),
	)
	return frappe._dict(warehouse=warehouse, level=level, qty=qty, type=material_request_type)


def _num(v):
	"""10 not 10.0, 2.5 not 2.500."""
	return ("%.3f" % flt(v)).rstrip("0").rstrip(".")


def _touched(item_code, note):
	"""The item's form shows who changed its reorder levels and when; marking it changed also stops
	an item form opened before from saving the old rows back over the new ones."""
	frappe.db.set_value("Item", item_code, "modified_by", frappe.session.user, update_modified=False)
	frappe.db.set_value("Item", item_code, "modified", now(), update_modified=False)
	frappe.get_doc("Item", item_code).add_comment("Info", note)
	frappe.clear_document_cache("Item", item_code)
