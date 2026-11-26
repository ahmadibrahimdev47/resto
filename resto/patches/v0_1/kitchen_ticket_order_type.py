import frappe


def execute():
	"""D-273: a kitchen ticket shows its order type in the list. Tickets from before it was kept take
	it from their order; a staff meal is its own type."""
	frappe.db.sql(
		"""
		update `tabResto Kitchen Ticket` t
		join `tabResto Order` o on o.name = t.resto_order
		set t.order_type = if(ifnull(o.staff_meal, 0) = 1, 'Staff Meal', o.order_type)
		where ifnull(t.order_type, '') = ''
		"""
	)
