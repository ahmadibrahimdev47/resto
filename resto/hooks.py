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
		"route": "/restaurant",
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
		# A return never takes packaging or consumables back into stock (D-217, D-224).
		"before_validate": "resto.resto.packaging.drop_from_return",
		"validate": "resto.resto.billing.validate_sales_invoice",
		"before_submit": "resto.resto.billing.before_sales_invoice_submit",
		"before_cancel": "resto.resto.billing.before_sales_invoice_cancel",
		"on_cancel": "resto.resto.billing.on_sales_invoice_cancel",
	},
	# A customer's debt paid to a cashier with an open shift goes into that shift's drawer.
	# A cash account taken below zero is warned about or refused (D-257).
	"Payment Entry": {
		"before_submit": "resto.resto.shift.link_payment_entry",
		"on_submit": "resto.resto.cash_guard.check_posting",
		"before_cancel": "resto.resto.shift.check_cancel",
	},
	# A salary entry's Journal Entry is cancelled with the salary entry (D-249).
	"Journal Entry": {
		"on_submit": "resto.resto.cash_guard.check_posting",
		"before_cancel": [
			"resto.resto.shift.check_cancel",
			"resto.resto.doctype.resto_salary_entry.resto_salary_entry.check_journal_cancel",
		]
	},
	# A cash purchase from a closed shift's drawer stays as the shift counted it (D-225).
	"Purchase Invoice": {
		"on_submit": "resto.resto.cash_guard.check_posting",
		"before_cancel": "resto.resto.shift.check_cancel",
	},
	"Stock Entry": {"before_cancel": "resto.resto.staff_meal.check_cancel"},
}

# Recipe costs follow purchase prices: refresh them every night.
# A password sign-in opens a lock screen PIN locked by wrong tries (D-216).
on_login = "resto.resto.approval.on_login"

scheduler_events = {
	"daily": ["resto.api.refresh_recipe_costs"],
	# The nightly backup and its off-site copy (D-256): managers are told once a day while late.
	"hourly": ["resto.resto.backup_watch.check"],
}

# The shift print format reads the items sold and the debts collected (D-206).
jinja = {"methods": ["resto.resto.shift.shift_print_data", "resto.resto.sides.sides_html"]}
