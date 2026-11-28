def execute():
	"""D-294: a paid dine-in table stays taken until it is freed. The orders the old rule freed at
	payment (every dish served) never had table_freed set: mark them freed, so the floor does not
	show them taken again."""
	import frappe

	frappe.db.sql(
		"""update `tabResto Order` set table_freed = 1
		where status = 'Paid' and order_type = 'Dine In' and ifnull(table_freed, 0) = 0
			and ifnull(paid_before_served, 0) = 0"""
	)
