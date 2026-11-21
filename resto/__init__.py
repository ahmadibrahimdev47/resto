import frappe

__version__ = "0.2.0"


def check_app_permission():
	"""Show the app on the apps screen to restaurant staff and to users who can read recipes."""
	from resto.resto.utils import ALL_ROLES

	if set(frappe.get_roles()) & set(ALL_ROLES):
		return True
	return frappe.has_permission("Resto Recipe", "read")
