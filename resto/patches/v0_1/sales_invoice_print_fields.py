from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	"""The receipt printed flag and the sides of each invoice line, created during the patches too: a
	payment reads them, and a migrate stopped by another app's patch would not reach after_migrate."""
	from resto.setup.install import SALES_INVOICE_FIELDS

	create_custom_fields(SALES_INVOICE_FIELDS, update=True)
