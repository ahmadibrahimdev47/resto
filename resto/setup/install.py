"""Roles and custom fields that Resto needs on every site (install and migrate)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint

from resto.resto.utils import ALL_ROLES, CASHIER, HOST, KITCHEN, MANAGER, WAITER

# What the Resto roles need on standard doctypes: ERPNext reads Item and Customer while it builds
# a bill, the cashier prints the receipt, the manager runs the reports on invoices and stock.
# The floor gets "select" only on Employee (the delivery driver link), so no HR data is readable.
STANDARD_PERMISSIONS = {
	"Item": {WAITER: ["read"], CASHIER: ["read"], HOST: ["read"], MANAGER: ["read"], KITCHEN: ["read"]},
	# The kitchen's waste and prep forms link a Company; its item lists filter by Item Group and
	# show the UOM. It runs no report (D-242).
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
	# A waste row is typed in a unit the item converts to (D-250): the kitchen links the unit.
	"UOM": {KITCHEN: ["select"]},
	# The Employee form reads HR Settings on load and on save (hrms): read only, none of its settings
	# change (D-248).
	"HR Settings": {MANAGER: ["read"]},
	"Sales Invoice": {CASHIER: ["read", "print"], MANAGER: ["read", "print", "report"]},
	# The kitchen runs Negative Prepared Stock (its prepared items' stock).
	# Not the kitchen: a Bin holds the stock value (D-242).
	"Bin": {MANAGER: ["read", "report"]},
	# ERPNext checks the customer's receivable account (select) while it builds any bill.
	"Account": {WAITER: ["select"], CASHIER: ["select"], MANAGER: ["select"]},
	# ... and reads the POS Profile (item details) while it builds the bill and its preview.
	"POS Profile": {WAITER: ["read"], CASHIER: ["read"], MANAGER: ["read"]},
	"Purchase Receipt": {MANAGER: ["read", "report"]},
	# Cash purchases from the drawer (D-225): the manager picks the cash supplier and opens the invoice.
	"Supplier": {MANAGER: ["select"]},
	"Purchase Invoice": {MANAGER: ["read"]},
	# The weekly stock count (D-244): the manager counts and posts it (the difference goes to the
	# company's Stock Adjustment Account), without Stock Manager, which opens items, warehouses and
	# stock settings. Not the kitchen: a count carries the stock's value (D-242).
	"Stock Reconciliation": {MANAGER: ["read", "write", "create", "submit", "cancel", "amend", "print", "report"]},
}

# A customer's debt paid to the cashier during a shift is counted by that shift's drawer (D-203).
# Each worker's fixed monthly salary, for the month's salary entry (D-249): money, so at permlevel 1
# (read by the roles that read a worker's record, never by the floor). HRMS's CTC is a yearly
# cost to the company, not the monthly pay.
EMPLOYEE_FIELDS = {
	"Employee": [
		{
			"fieldname": "resto_monthly_salary",
			"fieldtype": "Currency",
			"label": "Monthly Salary",
			"insert_after": "ctc",
			"permlevel": 1,
			"description": "The fixed pay of a month, taken by the month's salary entry (Monthly Salaries)",
		}
	]
}

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
		# Extra portions given free (D-253): billed at no price, stock still issued; the value at
		# the extra price and who gave it, for Resto Voids and Discounts.
		{
			"fieldname": "resto_free_extra",
			"fieldtype": "Check",
			"label": "Free Extra",
			"insert_after": "resto_packaging",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "resto_free_value",
			"fieldtype": "Currency",
			"label": "Free Extra Value",
			"insert_after": "resto_free_extra",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "resto_free_by",
			"fieldtype": "Link",
			"options": "User",
			"label": "Free Extra Given By",
			"insert_after": "resto_free_value",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
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
	create_custom_fields(EMPLOYEE_FIELDS, update=True)
	from resto.resto.item_recipe import item_fields

	create_custom_fields(item_fields(), update=True)
	set_new_setting_defaults()
	grant_standard_permissions()
	keep_money_from_the_kitchen()
	show_stock_row_expense_account()
	add_accounting_summary_links()
	sync_translations()


# A Stock Entry row's Difference Account («حساب الفرق») as a column of the items grid (D-254): on
# a Material Issue of supplies it is the expense the issue is booked to, and inside the row's
# panel it was left as the default. The grid keeps within Frappe's 10 columns: numbers are narrowed
# first, then the least needed column leaves the grid (never the form).
GRID_COLUMNS = 10
STOCK_ROW = "Stock Entry Detail"
STOCK_ROW_NARROW = ("qty", "transfer_qty", "basic_rate")
STOCK_ROW_DROP = ("basic_amount", "amount", "valuation_rate", "basic_rate", "uom", "transfer_qty")


def _grid_width(df):
	"""Grid columns a field takes (Frappe's defaults when none is set)."""
	if cint(df.columns):
		return cint(df.columns)
	if df.fieldtype == "Check":
		return 1
	if df.fieldtype in ("Text", "Small Text", "Text Editor", "Long Text"):
		return 3
	return 2


def show_stock_row_expense_account():
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	def put(fieldname, prop, value, kind):
		make_property_setter(STOCK_ROW, fieldname, prop, value, kind, for_doctype=False, validate_fields_for_doctype=False)

	if not frappe.get_meta(STOCK_ROW).get_field("expense_account"):
		return
	put("expense_account", "in_list_view", 1, "Check")
	put("expense_account", "columns", 2, "Int")
	frappe.clear_cache(doctype=STOCK_ROW)
	widths = {df.fieldname: _grid_width(df) for df in frappe.get_meta(STOCK_ROW).fields if df.in_list_view}
	for fieldname in STOCK_ROW_NARROW:
		if sum(widths.values()) <= GRID_COLUMNS:
			break
		if widths.get(fieldname, 0) > 1:
			widths[fieldname] = 1
			put(fieldname, "columns", 1, "Int")
	for fieldname in STOCK_ROW_DROP:
		if sum(widths.values()) <= GRID_COLUMNS:
			break
		if fieldname in widths:
			widths.pop(fieldname)
			put(fieldname, "in_list_view", 0, "Check")
	frappe.clear_cache(doctype=STOCK_ROW)


# The kitchen has nothing to do with bills or money (D-242). Rights Resto gave it before, taken
# back on every migrate: a Bin holds the stock value and its rate.
KITCHEN_TAKEN_BACK = ("Bin",)
# Doctypes the kitchen only links (select), even where a site customised their rights.
KITCHEN_SELECT_ONLY = ("Resto Order",)
# Item's money, moved to permlevel 1: every role that reads Item at level 0 reads it at level 1
# too (and writes it where it writes level 0), except the kitchen.
ITEM_MONEY = ("valuation_rate", "standard_rate", "last_purchase_rate", "prices_html")
# Resto's doctypes whose money is at permlevel 1 in their JSON: a site that customised their
# rights (Custom DocPerm) gets the same level 1 rows.
MONEY_DOCTYPES = ("Item", "Resto Recipe", "Resto Waste", "Employee")


def keep_money_from_the_kitchen():
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter
	from frappe.permissions import setup_custom_perms

	for doctype in KITCHEN_TAKEN_BACK:
		if frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": KITCHEN}):
			frappe.db.delete("Custom DocPerm", {"parent": doctype, "role": KITCHEN})
			frappe.clear_cache(doctype=doctype)

	for doctype in KITCHEN_SELECT_ONLY:
		for name in frappe.get_all("Custom DocPerm", filters={"parent": doctype, "role": KITCHEN}, pluck="name"):
			frappe.db.set_value(
				"Custom DocPerm", name, {"select": 1, **dict.fromkeys((*DEFAULT_RIGHTS, *OTHER_RIGHTS), 0)}
			)
			frappe.clear_cache(doctype=doctype)

	meta = frappe.get_meta("Item")
	for fieldname in ITEM_MONEY:
		df = meta.get_field(fieldname)
		if df and not df.permlevel:
			make_property_setter("Item", fieldname, "permlevel", 1, "Int", validate_fields_for_doctype=False)
	setup_custom_perms("Item")  # Item's level 1 rows live with its other custom rows

	for doctype in MONEY_DOCTYPES:
		if frappe.db.exists("DocType", doctype) and frappe.db.exists("Custom DocPerm", {"parent": doctype}):
			_mirror_level_one(doctype)


def _mirror_level_one(doctype):
	"""Every role that reads the doctype at level 0 reads level 1 too (writes it where it writes
	level 0); the kitchen holds no level 1 row."""
	rows = frappe.get_all(
		"Custom DocPerm", filters={"parent": doctype, "permlevel": 0, "if_owner": 0}, fields=["role", "read", "write"]
	)
	for r in rows:
		if r.role == KITCHEN or not r.read:
			continue
		have = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": r.role, "permlevel": 1}, ["name", "write"], as_dict=True)
		if not have:
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": r.role,
					"permlevel": 1,
					"read": 1,
					"write": r.write,
				}
			).insert(ignore_permissions=True)
		elif r.write and not have.write:
			frappe.db.set_value("Custom DocPerm", have.name, "write", 1)
	frappe.db.delete("Custom DocPerm", {"parent": doctype, "role": KITCHEN, "permlevel": 1})
	frappe.clear_cache(doctype=doctype)


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
	defaults = [("vat_on_service", 1), ("negative_cash", "Warn Only")]
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
