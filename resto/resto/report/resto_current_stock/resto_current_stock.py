"""What each store holds now, and what came in and went out over the dates (D-285); and what is
short of the reorder level set on the item for that store (D-287).

ERPNext's Stock Balance does the counting; this keeps the columns a storekeeper reads, in that
order. It runs at once (not as a prepared report in the background), as the data is small.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

# (Stock Balance's column or this report's, its label here, type, width)
COLUMNS = (
	("item_code", "Item", "Link", 170),
	("item_name", "Item Name", "Data", 170),
	("item_group", "Item Group", "Link", 140),
	("warehouse", "Warehouse", "Link", 140),
	("stock_uom", "UOM", "Link", 70),
	("in_qty", "In Qty", "Float", 100),
	("out_qty", "Out Qty", "Float", 100),
	("bal_qty", "Balance Qty", "Float", 110),
	("reorder_level", "Reorder Level", "Float", 100),
	("to_order", "Suggested Purchase Qty", "Float", 120),
	("val_rate", "Valuation Rate", "Currency", 110),
	("bal_val", "Balance Value", "Currency", 120),
)
OPTIONS = {"item_code": "Item", "item_group": "Item Group", "warehouse": "Warehouse", "stock_uom": "UOM"}


def execute(filters=None):
	from erpnext.stock.report.stock_balance.stock_balance import execute as stock_balance

	filters = frappe._dict(filters or {})
	if not filters.get("company"):
		frappe.throw(_("Choose the company"))
	filters.to_date = filters.get("to_date") or nowdate()
	filters.from_date = filters.get("from_date") or filters.to_date
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("From Date must be before To Date"))
	asked = frappe._dict({k: v for k, v in filters.items() if k in ("company", "from_date", "to_date", "warehouse", "item_group", "item_code") and v})
	result = stock_balance(asked)
	rows = [frappe._dict(r) for r in (result[1] if result and len(result) > 1 else []) if isinstance(r, dict) and r.get("item_code")]

	levels = _reorder_levels(filters)
	# An item with a reorder level for a store and nothing there is the most short of all: Stock
	# Balance leaves it out (no stock, no movement), so it is added at zero.
	seen = {(r.item_code, r.warehouse) for r in rows}
	for (item_code, warehouse), level in levels.items():
		if (item_code, warehouse) not in seen:
			rows.append(
				frappe._dict(
					item_code=item_code,
					item_name=level.item_name,
					item_group=level.item_group,
					warehouse=warehouse,
					stock_uom=level.stock_uom,
					in_qty=0,
					out_qty=0,
					bal_qty=0,
					val_rate=0,
					bal_val=0,
				)
			)
	for r in rows:
		level = levels.get((r.item_code, r.warehouse))
		if level:
			r.reorder_level = level.level
			r.short = flt(r.bal_qty) <= level.level
			if r.short:
				# The reorder quantity set on the item, or at least what brings it back to the level.
				r.to_order = max(level.qty, level.level - flt(r.bal_qty))
	if cint(filters.get("short_only")):
		rows = [r for r in rows if r.get("short")]
	rows.sort(key=lambda r: (not r.get("short"), r.item_code, r.warehouse))

	# A site whose item codes are its names does not need the names twice.
	keep_name = any((r.get("item_name") or r.get("item_code")) != r.get("item_code") for r in rows)
	columns = []
	for fieldname, label, fieldtype, width in COLUMNS:
		if fieldname == "item_name" and not keep_name:
			continue
		# Its own wording here (a context), not ERPNext's for the same words elsewhere.
		column = {"fieldname": fieldname, "label": _(label, context="Resto Current Stock"), "fieldtype": fieldtype, "width": width}
		if fieldname in OPTIONS:
			column["options"] = OPTIONS[fieldname]
		columns.append(column)
	data = [{**{c["fieldname"]: r.get(c["fieldname"]) for c in columns}, "short": 1 if r.get("short") else 0} for r in rows]
	# Totals of the value and the short items: quantities in different units are not added up.
	currency = frappe.get_cached_value("Company", filters.company, "default_currency")
	summary = [
		{"value": sum(flt(r.get("bal_val")) for r in rows), "label": _("Balance Value", context="Resto Current Stock"), "datatype": "Currency", "currency": currency},
		{"value": sum(1 for r in rows if r.get("short")), "label": _("Short Items"), "datatype": "Int", "indicator": "Red"},
	]
	return columns, data, None, None, summary


def _reorder_levels(filters):
	"""{(item, warehouse): level, qty, item details}: the reorder levels set on the items (Item
	Reorder, for one store), within the report's company, store, item group and item."""
	conditions, values = ["w.company = %(company)s", "i.disabled = 0", "ir.warehouse_reorder_level > 0"], {"company": filters.company}
	if filters.get("warehouse"):
		lft, rgt = frappe.db.get_value("Warehouse", filters.warehouse, ["lft", "rgt"])
		conditions.append("w.lft >= %(wlft)s and w.rgt <= %(wrgt)s")
		values.update(wlft=lft, wrgt=rgt)
	if filters.get("item_group"):
		lft, rgt = frappe.db.get_value("Item Group", filters.item_group, ["lft", "rgt"])
		conditions.append("exists (select 1 from `tabItem Group` g where g.name = i.item_group and g.lft >= %(glft)s and g.rgt <= %(grgt)s)")
		values.update(glft=lft, grgt=rgt)
	if filters.get("item_code"):
		conditions.append("i.name = %(item_code)s")
		values["item_code"] = filters.item_code
	rows = frappe.db.sql(
		f"""select ir.parent as item_code, ir.warehouse, ir.warehouse_reorder_level as level, ir.warehouse_reorder_qty as qty,
			i.item_name, i.item_group, i.stock_uom
		from `tabItem Reorder` ir
		join `tabItem` i on i.name = ir.parent
		join `tabWarehouse` w on w.name = ir.warehouse
		where ir.parenttype = 'Item' and {" and ".join(conditions)}""",
		values,
		as_dict=True,
	)
	return {(r.item_code, r.warehouse): frappe._dict(r, level=flt(r.level), qty=flt(r.qty)) for r in rows}
