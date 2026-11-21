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
		"route": "/desk/resto-floor",
		"has_permission": "resto.check_app_permission",
	}
]
# Frappe v16 shows only the first add_to_apps_screen entry of each app, so the POSNext
# cashier screen (/pos) cannot get its own icon from here.

# Design rules (see README):
# - No override_doctype_class, no override_whitelisted_methods, no monkey patches.
#   POSNext already overrides Sales Invoice and packed-item code; we must not collide.
# - Custom fields we add to standard doctypes are prefixed with "resto_".

after_install = "resto.setup.install.after_install"
after_migrate = "resto.setup.install.after_migrate"

doc_events = {
	"Sales Invoice": {
		"validate": "resto.resto.billing.validate_sales_invoice",
		"before_cancel": "resto.resto.billing.before_sales_invoice_cancel",
		"on_cancel": "resto.resto.billing.on_sales_invoice_cancel",
	},
	# POSNext shift closing must include every bill settled on the floor.
	"POS Closing Shift": {
		"validate": "resto.resto.billing.validate_closing_shift",
	},
}

# Recipe costs follow purchase prices: refresh them every night.
scheduler_events = {"daily": ["resto.api.refresh_recipe_costs"]}
