import frappe


def execute():
	"""Receipts print with Resto's receipt format (copy labels, sides per copy, order and kitchen
	ticket numbers) unless another format is already chosen."""
	if frappe.db.get_single_value("Resto Settings", "receipt_print_format"):
		return
	if frappe.db.exists("Print Format", "Resto Receipt"):
		frappe.db.set_single_value("Resto Settings", "receipt_print_format", "Resto Receipt")
