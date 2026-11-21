import frappe

__version__ = "0.1.0"


def check_app_permission():
	"""Show the app on the apps screen to users who can read recipes."""
	return frappe.has_permission("Resto Recipe", "read")
