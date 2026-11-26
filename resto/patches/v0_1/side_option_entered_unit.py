import frappe


def execute():
	"""D-264: a side option's portion is typed in a unit of its own now. A row from before keeps its
	quantity, read as typed in the item's stock unit."""
	frappe.db.sql(
		"""
		update `tabResto Side Option` o
		join `tabItem` i on i.name = o.item
		set o.entered_qty = o.portion_qty, o.entered_uom = i.stock_uom, o.uom = i.stock_uom
		where ifnull(o.entered_qty, 0) = 0 and ifnull(o.portion_qty, 0) > 0
		"""
	)
