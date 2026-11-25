import frappe

# Customer fields POSNext shipped (its custom/customer.json) for its e-mail check (D-208).
FIELDS = ("custom_email_verified", "custom_email_verified_by", "custom_email_verified_on", "custom_verified_email")


def execute():
	"""Removed once POSNext is uninstalled, and only while none of them holds a value: data the site
	typed is never dropped with a column."""
	if "pos_next" in frappe.get_installed_apps():
		return
	removed = False
	for fieldname in FIELDS:
		name = frappe.db.get_value("Custom Field", {"dt": "Customer", "fieldname": fieldname}, "name")
		if not name:
			continue
		if frappe.db.has_column("Customer", fieldname) and frappe.db.sql(
			f"select 1 from `tabCustomer` where ifnull(`{fieldname}`, '') not in ('', '0') limit 1"
		):
			continue
		frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		removed = True
	if removed:
		frappe.clear_cache(doctype="Customer")
