app_name = "resto"
app_title = "Resto"
app_publisher = "Resto"
app_description = "Restaurant back-office for ERPNext v16: recipes, daily prep, waste and food cost"
app_email = ""
app_license = "Proprietary"

# Apps
# ------------------

required_apps = ["erpnext"]

# Arabic font served locally (restaurants often work offline).
app_include_css = "/assets/resto/css/resto.css"
# Shared desk JS (Recipe Status dialog, food cost colors) as a bundle: every build gets a new
# hashed URL, so browsers never keep running an old copy.
app_include_js = "resto.bundle.js"
# Kitchen station ticket numbers of the day (kitchen._daily_no) survive bench clear-cache and migrate.
persistent_cache_keys = ["resto_kds_no:*"]

boot_session = "resto.boot.boot_session"

doctype_js = {"Item": "public/js/item_recipe.js"}

add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/resto/images/resto-logo.svg",
		"title": app_title,
		"route": "/desk/resto-floor",
		"has_permission": "resto.check_app_permission",
	}
]

# Design rules (see README):
# - No override_doctype_class, no override_whitelisted_methods, no monkey patches.
#   Resto keeps its own shift (Resto Shift, D-203); POSNext is not needed.
# - Custom fields we add to standard doctypes are prefixed with "resto_".

after_install = "resto.setup.install.after_install"
after_migrate = "resto.setup.install.after_migrate"
after_app_install = "resto.setup.install.after_app_install"

doc_events = {
	# The Recipe tab on Item edits the item's Resto Recipe (see resto/resto/item_recipe.py).
	"Item": {
		"before_insert": "resto.resto.item_variant.space_variant_code",
		"onload": "resto.resto.item_recipe.onload",
		"validate": "resto.resto.item_recipe.validate",
		"on_update": "resto.resto.item_recipe.on_update",
		"on_change": "resto.resto.item_recipe.on_change",
	},
	"Sales Invoice": {
		"validate": "resto.resto.billing.validate_sales_invoice",
		"before_submit": "resto.resto.billing.before_sales_invoice_submit",
		"before_cancel": "resto.resto.billing.before_sales_invoice_cancel",
		"on_cancel": "resto.resto.billing.on_sales_invoice_cancel",
	},
	# A customer's debt paid to a cashier with an open shift goes into that shift's drawer.
	"Payment Entry": {
		"before_submit": "resto.resto.shift.link_payment_entry",
		"before_cancel": "resto.resto.shift.check_cancel",
	},
	"Journal Entry": {"before_cancel": "resto.resto.shift.check_cancel"},
}

# Recipe costs follow purchase prices: refresh them every night.
scheduler_events = {"daily": ["resto.api.refresh_recipe_costs"]}

# The shift print format reads the items sold and the debts collected (D-206).
jinja = {"methods": ["resto.resto.shift.shift_print_data"]}
