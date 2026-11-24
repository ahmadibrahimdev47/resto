import frappe


def execute():
	"""A cashier with a network printer keeps printing on it; without one, bills and receipts keep
	printing from the browser by the cashier's click."""
	if frappe.db.get_single_value("Resto Settings", "cashier_print_method"):
		return
	printer = frappe.db.get_single_value("Resto Settings", "cashier_printer")
	frappe.db.set_single_value(
		"Resto Settings", "cashier_print_method", "Server Printer" if printer else "Browser Button"
	)
