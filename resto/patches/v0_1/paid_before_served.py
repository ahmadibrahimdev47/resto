import frappe
from frappe.utils import add_to_date, now_datetime


def execute():
	"""D-276: a dine-in order paid before its dishes were served keeps its table until it is freed.
	The tables the old rule still keeps (a dish not ready yet) are such orders; the ones it already
	freed stay free. Dishes left ready by a payment that served their tickets are served."""
	since = add_to_date(now_datetime(), hours=-12)
	frappe.db.sql(
		"""
		update `tabResto Order Item` i
		join `tabResto Kitchen Ticket Item` k on k.order_item = i.name
		join `tabResto Kitchen Ticket` t on t.name = k.parent
		join `tabResto Order` o on o.name = i.parent
		set i.kitchen_status = 'Served'
		where o.status = 'Paid' and i.kitchen_status = 'Ready' and t.status = 'Served' and o.opened_at >= %s
		""",
		since,
	)
	frappe.db.sql(
		"""
		update `tabResto Order` o
		set o.paid_before_served = 1
		where o.status = 'Paid' and o.order_type = 'Dine In' and ifnull(o.table_freed, 0) = 0 and o.opened_at >= %s
			and exists (select 1 from `tabResto Order Item` i where i.parent = o.name
				and i.kitchen_status not in ('Ready', 'Served', 'Void'))
		""",
		since,
	)
