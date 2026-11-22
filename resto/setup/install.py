"""Roles and custom fields that Resto needs on every site (install and migrate)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from resto.resto.utils import ALL_ROLES, CASHIER, HOST, MANAGER, WAITER

# What the Resto roles need on standard doctypes: ERPNext reads Item and Customer while it builds
# a bill, the cashier prints the receipt, the manager runs the reports on invoices and stock.
# Employee is "select" only (the delivery driver link), so no HR data is readable.
STANDARD_PERMISSIONS = {
	"Item": {WAITER: ["read"], CASHIER: ["read"], HOST: ["read"], MANAGER: ["read"]},
	"Customer": {WAITER: ["read"], CASHIER: ["read"], HOST: ["read"], MANAGER: ["read"]},
	"Employee": {CASHIER: ["select"], MANAGER: ["select"]},
	"Sales Invoice": {CASHIER: ["read", "print"], MANAGER: ["read", "print", "report"]},
	"Bin": {MANAGER: ["read", "report"]},
	"Purchase Receipt": {MANAGER: ["read", "report"]},
}

SALES_INVOICE_FIELDS = {
	"Sales Invoice": [
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
	]
}


def after_install():
	setup()


def after_migrate():
	setup()


def setup():
	make_roles()
	create_custom_fields(SALES_INVOICE_FIELDS, update=True)
	from resto.resto.item_recipe import ITEM_FIELDS

	create_custom_fields(ITEM_FIELDS, update=True)
	set_new_setting_defaults()
	grant_standard_permissions()


def grant_standard_permissions():
	"""Add the rights above once; rights the site already gives are left as they are."""
	from frappe.permissions import add_permission, update_permission_property

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


def set_new_setting_defaults():
	"""Settings added in later versions get their default once (a Single keeps no default by itself)."""
	for field, value in (("vat_on_service", 1),):
		if not frappe.db.sql("select 1 from `tabSingles` where doctype = %s and field = %s", ("Resto Settings", field)):
			frappe.db.set_single_value("Resto Settings", field, value)


def make_roles():
	for role in ALL_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
