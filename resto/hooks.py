app_name = "resto"
app_title = "Resto"
app_publisher = "Resto"
app_description = "Restaurant back-office for ERPNext v16: recipes, daily prep, waste and food cost"
app_email = ""
app_license = "Proprietary"

# Apps
# ------------------

required_apps = ["erpnext"]

add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/resto/images/resto-logo.svg",
		"title": app_title,
		"route": "/desk/resto-recipe",
		"has_permission": "resto.check_app_permission",
	},
	# POSNext ships without an apps-screen entry, so the cashier screen gets one here.
	{
		"name": app_name,
		"logo": "/assets/resto/images/pos-logo.svg",
		"title": "Point of Sale",
		"route": "/pos",
		"has_permission": "resto.check_pos_permission",
	},
]

# Design rules (see README):
# - No override_doctype_class, no override_whitelisted_methods, no monkey patches.
#   POSNext already overrides Sales Invoice and packed-item code; we must not collide.
# - Custom fields we add to standard doctypes are prefixed with "resto_".
