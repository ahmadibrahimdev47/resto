import frappe


def execute():
	"""Stations that had a network printer keep printing on it; the others keep the browser button."""
	for name, printer in frappe.get_all("Resto Kitchen Station", fields=["name", "printer"], as_list=True):
		frappe.db.set_value(
			"Resto Kitchen Station",
			name,
			"print_method",
			"Server Printer" if printer else "Browser Button",
			update_modified=False,
		)
