from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	"""The customer's delivery address, created during the patches too: the floor searches it."""
	from resto.setup.install import CUSTOMER_FIELDS

	create_custom_fields(CUSTOMER_FIELDS, update=True)
