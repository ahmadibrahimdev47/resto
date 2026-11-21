import frappe

__version__ = "0.1.0"


def check_app_permission():
	"""Show the app on the apps screen to users who can read recipes."""
	return frappe.has_permission("Resto Recipe", "read")


def check_pos_permission():
	"""Show the cashier screen icon when POSNext is installed and the user can sell."""
	return "pos_next" in frappe.get_installed_apps() and frappe.has_permission(
		"Sales Invoice", "create"
	)
