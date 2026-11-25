import frappe


def execute():
	"""POSNext is no longer used (D-203) and was uninstalled: its roles, the rights its fixtures gave
	them, and custom fields that link to doctypes it took with it are removed (D-208). Fields that
	still work (plain data on Customer) are left: they may hold the site's own data."""
	if "pos_next" in frappe.get_installed_apps():
		return
	from resto.setup.install import FIXTURE_ROLES

	doctypes = set(frappe.get_all("Custom DocPerm", filters={"role": ("in", FIXTURE_ROLES)}, pluck="parent"))
	frappe.db.delete("Custom DocPerm", {"role": ("in", FIXTURE_ROLES)})
	frappe.db.delete("Has Role", {"role": ("in", FIXTURE_ROLES)})
	for role in FIXTURE_ROLES:
		if frappe.db.exists("Role", role):
			frappe.delete_doc("Role", role, force=True, ignore_permissions=True)

	for f in frappe.get_all(
		"Custom Field",
		filters={"fieldtype": ("in", ("Link", "Table", "Table MultiSelect"))},
		fields=["name", "dt", "options"],
	):
		if f.options and not frappe.db.exists("DocType", f.options):
			frappe.delete_doc("Custom Field", f.name, force=True, ignore_permissions=True)
			doctypes.add(f.dt)

	for doctype in doctypes:
		frappe.clear_cache(doctype=doctype)
