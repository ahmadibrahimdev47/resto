"""/restaurant: where the app icon of the apps screen lands (D-240).

Each user goes to the first restaurant page they may open: the floor for waiters, cashiers, hosts
and managers, the kitchen screen for the kitchen, otherwise the recipes or the desk. A fixed route
sent the kitchen to the floor, which it is not allowed to open.
"""

from urllib.parse import quote

import frappe

no_cache = 1

# In order: the first one the user may open.
PAGES = ("resto-floor", "resto-kitchen", "resto-analytics")


def landing_route():
	for page in PAGES:
		if frappe.db.exists("Page", page) and frappe.get_doc("Page", page).is_permitted():
			return f"/desk/{page}"
	if frappe.has_permission("Resto Recipe", "read"):
		return "/desk/resto-recipe"
	return "/desk"


def get_context(context):
	if frappe.session.user == "Guest":
		location = "/login?redirect-to=" + quote("/restaurant")
	else:
		location = landing_route()
	frappe.local.flags.redirect_location = location
	raise frappe.Redirect
