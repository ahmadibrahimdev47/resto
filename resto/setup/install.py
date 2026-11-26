"""Roles and custom fields that Resto needs on every site (install and migrate)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from resto.resto.utils import ALL_ROLES, CASHIER, HOST, KITCHEN, MANAGER, WAITER

# What the Resto roles need on standard doctypes: ERPNext reads Item and Customer while it builds
# a bill, the cashier prints the receipt, the manager runs the reports on invoices and stock.
# The floor gets "select" only on Employee (the delivery driver link), so no HR data is readable.
STANDARD_PERMISSIONS = {
	"Item": {WAITER: ["read"], CASHIER: ["read"], HOST: ["read"], MANAGER: ["read"], KITCHEN: ["read"]},
	# The kitchen runs the Prep Forecast: its Company and Item Group filters. A report checks
	# read on the doctype of every Link column it shows (Item Group, UOM).
	"Company": {KITCHEN: ["select"]},
	"Item Group": {KITCHEN: ["read"], MANAGER: ["read"]},
	"UOM": {KITCHEN: ["read"], MANAGER: ["read"]},
	"Customer": {WAITER: ["read"], CASHIER: ["read"], HOST: ["read"], MANAGER: ["read"]},
	# The manager keeps the workers' records (D-238): their staff advances are owed by name. The
	# cashier only picks one (the advance dialog's list comes from the server). Not HR User: no
	# leave, attendance or payroll. Gender and Designation are the form's links.
	"Employee": {CASHIER: ["select"], MANAGER: ["select", "read", "write", "create"]},
	"Gender": {MANAGER: ["select"]},
	"Designation": {MANAGER: ["select"]},
	"Sales Invoice": {CASHIER: ["read", "print"], MANAGER: ["read", "print", "report"]},
	# The kitchen runs Negative Prepared Stock (its prepared items' stock).
	"Bin": {MANAGER: ["read", "report"], KITCHEN: ["read", "report"]},
	# ERPNext checks the customer's receivable account (select) while it builds any bill.
	"Account": {WAITER: ["select"], CASHIER: ["select"], MANAGER: ["select"]},
	# ... and reads the POS Profile (item details) while it builds the bill and its preview.
	"POS Profile": {WAITER: ["read"], CASHIER: ["read"], MANAGER: ["read"]},
	"Purchase Receipt": {MANAGER: ["read", "report"]},
	# Cash purchases from the drawer (D-225): the manager picks the cash supplier and opens the invoice.
	"Supplier": {MANAGER: ["select"]},
	"Purchase Invoice": {MANAGER: ["read"]},
}

# A customer's debt paid to the cashier during a shift is counted by that shift's drawer (D-203).
PAYMENT_ENTRY_FIELDS = {
	"Payment Entry": [
		{
			"fieldname": "resto_shift",
			"fieldtype": "Link",
			"label": "Restaurant Shift",
			"options": "Resto Shift",
			"insert_after": "mode_of_payment",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"search_index": 1,
		}
	]
}

# The delivery address typed at the counter, kept on the customer as typed (an ERPNext Address
# needs a city and country the cashier does not type).
CUSTOMER_FIELDS = {
	"Customer": [
		{
			"fieldname": "resto_address",
			"fieldtype": "Small Text",
			"label": "Delivery Address",
			"insert_after": "customer_primary_address",
			"translatable": 0,
		}
	]
}

SALES_INVOICE_FIELDS = {
	"Sales Invoice": [
		# The cashier's shift the bill was settled in (D-203): its closing counts this invoice.
		{
			"fieldname": "resto_shift",
			"fieldtype": "Link",
			"label": "Restaurant Shift",
			"options": "Resto Shift",
			"insert_after": "pos_profile",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"search_index": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "resto_section",
			"fieldtype": "Section Break",
			"label": "Restaurant",
			"insert_after": "remarks",
			"collapsible": 1,
			"depends_on": "eval:doc.resto_order",
		},
		{
			"fieldname": "resto_order",
			"fieldtype": "Link",
			"label": "Restaurant Order",
			"options": "Resto Order",
			"insert_after": "resto_section",
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
		},
		{
			"fieldname": "resto_order_type",
			"fieldtype": "Data",
			"label": "Order Type",
			"insert_after": "resto_order",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "resto_table",
			"fieldtype": "Link",
			"label": "Table",
			"options": "Resto Table",
			"insert_after": "resto_order_type",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "resto_column_break",
			"fieldtype": "Column Break",
			"insert_after": "resto_table",
		},
		{
			"fieldname": "resto_waiter",
			"fieldtype": "Link",
			"label": "Waiter",
			"options": "User",
			"insert_after": "resto_column_break",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "resto_guests",
			"fieldtype": "Int",
			"label": "Guests",
			"insert_after": "resto_waiter",
			"read_only": 1,
			"no_copy": 1,
		},
		# Set once the receipt printed (on the cashier printer, or by the cashier browser), so a
		# retried payment never prints it twice; Print Again still prints it.
		{
			"fieldname": "resto_receipt_printed",
			"fieldtype": "Check",
			"label": "Receipt Printed",
			"insert_after": "resto_guests",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"print_hide": 1,
		},
	],
	# The sides a dish was served with when they differ from its recipe (resto/resto/sides.py).
	# Copied to a return, which takes back what was served.
	"Sales Invoice Item": [
		{
			"fieldname": "resto_sides",
			"fieldtype": "Code",
			"label": "Sides Served",
			"insert_after": "description",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
			"report_hide": 1,
		},
		# Their text (with / without), printed by Resto Receipt on the copies that show the sides.
		{
			"fieldname": "resto_sides_text",
			"fieldtype": "Small Text",
			"label": "Sides Text",
			"insert_after": "resto_sides",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
			"report_hide": 1,
		},
		# A takeaway or delivery packaging line (D-217): per dish or once per order. Copied to a
		# return, which drops the line: packaging used is never put back in stock.
		{
			"fieldname": "resto_packaging",
			"fieldtype": "Select",
			"label": "Packaging",
			"options": "\nDish\nOrder",
			"insert_after": "resto_sides_text",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
			"report_hide": 1,
		},
	],
}


def after_install():
	setup()


def after_migrate():
	setup()


def setup():
	make_roles()
	create_custom_fields(SALES_INVOICE_FIELDS, update=True)
	create_custom_fields(CUSTOMER_FIELDS, update=True)
	create_custom_fields(PAYMENT_ENTRY_FIELDS, update=True)
	from resto.resto.item_recipe import item_fields

	create_custom_fields(item_fields(), update=True)
	set_new_setting_defaults()
	grant_standard_permissions()
	add_accounting_summary_links()
	sync_translations()


def after_app_install(app_name):
	# An app installed after Resto may override its wording (POSNext's "Submit").
	if app_name != "resto":
		sync_translations()
	if app_name == "pos_next":
		repair_fixture_permissions()


def sync_translations():
	from resto.setup.translations import sync

	sync()


# Roles another app gives rights to through Custom DocPerm fixtures (POSNext).
FIXTURE_ROLES = ("POSNext Cashier", "Nexus POS Manager")


def repair_fixture_permissions():
	"""Once a doctype has a Custom DocPerm, Frappe reads only those rows. POSNext ships rows for its
	own roles as fixtures, without the doctype's standard rules (Frappe copies those first only when
	rights are added through Role Permission Manager), so a doctype it touches is left without them:
	no one can create a POS Profile. A doctype POSNext touched whose custom rows hold none of its standard
	roles (only POSNext's, and rights added after them) gets its standard rules copied in, as Frappe
	would have; one that has any of them is left as it is."""
	rows = frappe.get_all("Custom DocPerm", filters={"role": ("in", FIXTURE_ROLES)}, pluck="parent", distinct=True)
	for doctype in set(rows):
		roles = set(frappe.get_all("Custom DocPerm", filters={"parent": doctype}, pluck="role"))
		standard = frappe.get_all("DocPerm", filters={"parent": doctype, "parenttype": "DocType"}, fields="*")
		if roles & {d.role for d in standard}:
			continue  # the standard rules are there (copied by Frappe, or the site's own choice)
		for d in standard:
			if d.role in FIXTURE_ROLES:
				continue
			perm = frappe.new_doc("Custom DocPerm")
			perm.update({k: v for k, v in d.items() if k not in ("name", "creation", "modified", "modified_by", "owner")})
			perm.parent, perm.parenttype, perm.parentfield = doctype, "DocType", "permissions"
			perm.insert(ignore_permissions=True)
		frappe.clear_cache(doctype=doctype)


def grant_standard_permissions():
	"""Add the rights above once; rights the site already gives are left as they are."""
	from frappe.permissions import add_permission, update_permission_property

	repair_fixture_permissions()

	for doctype, roles in STANDARD_PERMISSIONS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for role, rights in roles.items():
			if not frappe.db.exists("Role", role):
				continue
			row = frappe.db.get_value(
				"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}, rights, as_dict=True
			)
			if not row:
				add_permission(doctype, role, 0, rights[0])
				row = frappe._dict()
			for right in rights:
				if not row.get(right):
					update_permission_property(doctype, role, 0, right, 1, validate=False)
			if "read" not in rights:
				_select_only(doctype, role, rights)


# A new Custom DocPerm row comes with these by default (the DocPerm field defaults).
DEFAULT_RIGHTS = ("read", "export")
# Rights a Custom DocPerm row can carry besides select and the defaults.
OTHER_RIGHTS = ("write", "create", "delete", "submit", "cancel", "amend", "report", "import", "print", "email", "share")


def _select_only(doctype, role, rights):
	"""A new Custom DocPerm row gets read and export by default; where only select was meant
	(Account: the floor's bills check it, staff need not browse or export the chart of accounts)
	they are taken off. Only a row that holds nothing else, so a right the site gave on purpose stays."""
	row = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0},
		["name", *DEFAULT_RIGHTS, *OTHER_RIGHTS],
		as_dict=True,
	)
	if not row or any(row.get(r) for r in OTHER_RIGHTS if r not in rights):
		return
	extra = {r: 0 for r in DEFAULT_RIGHTS if row.get(r) and r not in rights}
	if extra:
		frappe.db.set_value("Custom DocPerm", row.name, extra)
		frappe.clear_cache(doctype=doctype)


def set_new_setting_defaults():
	"""Settings added in later versions get their default once (a Single keeps no default by itself)."""
	defaults = [("vat_on_service", 1)]
	# Resto's receipt prints the copy labels, sides per copy and the kitchen ticket numbers.
	if frappe.db.exists("Print Format", "Resto Receipt"):
		defaults.append(("receipt_print_format", "Resto Receipt"))
	for field, value in defaults:
		if not frappe.db.sql("select 1 from `tabSingles` where doctype = %s and field = %s", ("Resto Settings", field)):
			frappe.db.set_single_value("Resto Settings", field, value)


def make_roles():
	for role in ALL_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


# The Accounting sidebar's Reports: each summary right after its detailed report (D-228). Added to
# ERPNext's own sidebar record on every migrate, and only where it is missing: ERPNext's files are
# never edited, a sidebar ERPNext rewrites gets them again, and the report's roles decide who sees it.
SUMMARY_LINKS = (("Accounts Payable", "Accounts Payable Summary"), ("Accounts Receivable", "Accounts Receivable Summary"))


def _sidebar_doctype():
	"""The v16 sidebar doctype, as Resto's own sidebar file names it."""
	import json
	import os

	path = os.path.join(os.path.dirname(__file__), "..", "resto", "sidebar", "resto", "resto.json")
	with open(path) as f:
		return json.load(f)["doctype"]


def add_accounting_summary_links():
	try:
		doctype = _sidebar_doctype()
		if not frappe.db.exists("DocType", doctype):
			return
		child = frappe.get_meta(doctype).get_field("items").options
	except Exception:
		return  # no sidebar of this kind on this version: nothing to add to
	added = False
	for after, report in SUMMARY_LINKS:
		if not frappe.db.exists("Report", report):
			continue
		for anchor in frappe.get_all(
			child,
			filters={"parenttype": doctype, "parentfield": "items", "link_type": "Report", "link_to": after},
			fields=["*"],
		):
			if frappe.db.exists(child, {"parent": anchor.parent, "parenttype": doctype, "link_to": report}):
				continue
			# Rows below make room; the new row copies its neighbour's place in the section.
			frappe.db.sql(
				f"update `tab{child}` set idx = idx + 1 where parent = %s and parenttype = %s and parentfield = 'items' and idx > %s",
				(anchor.parent, doctype, anchor.idx),
			)
			row = {k: v for k, v in anchor.items() if k not in ("name", "creation", "modified", "owner", "modified_by")}
			row.update({"doctype": child, "label": report, "link_to": report, "idx": anchor.idx + 1})
			frappe.get_doc(row).db_insert()
			added = True
	if added:
		frappe.clear_cache()
