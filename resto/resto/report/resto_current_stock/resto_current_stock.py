"""What each store holds now, and what came in and went out over the dates (D-285).

ERPNext's Stock Balance does the counting; this keeps the columns a storekeeper reads, in that
order. It runs at once (not as a prepared report in the background), as the data is small.
"""

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

# (Stock Balance's column, its label here, type, width)
COLUMNS = (
	("item_code", "Item", "Link", 170),
	("item_name", "Item Name", "Data", 170),
	("item_group", "Item Group", "Link", 140),
	("warehouse", "Warehouse", "Link", 140),
	("stock_uom", "UOM", "Link", 70),
	("in_qty", "In Qty", "Float", 100),
	("out_qty", "Out Qty", "Float", 100),
	("bal_qty", "Balance Qty", "Float", 110),
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
	rows = [frappe._dict(r) for r in (result[1] if result and len(result) > 1 else []) if isinstance(r, dict)]
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
	data = [{c["fieldname"]: r.get(c["fieldname"]) for c in columns} for r in rows]
	return columns, data
