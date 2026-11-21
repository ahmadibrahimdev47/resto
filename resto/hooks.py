app_name = "resto"
app_title = "Resto"
app_publisher = "Resto"
app_description = "Restaurant back-office for ERPNext v16: recipes, daily prep, waste and food cost"
app_email = ""
app_license = "Proprietary"

# Apps
# ------------------

required_apps = ["erpnext"]

# Design rules (see README):
# - No override_doctype_class, no override_whitelisted_methods, no monkey patches.
#   POSNext already overrides Sales Invoice and packed-item code; we must not collide.
# - Custom fields we add to standard doctypes are prefixed with "resto_".
