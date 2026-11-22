"""Variant codes and names with spaces around the dash: "Kofta - 1 Kg" instead of "Kofta-1 Kg".

In Arabic (right to left) ERPNext's "Kofta-1 Kg" reads as "-1". Only codes and names exactly as
ERPNext generates them are changed; a code or name the user typed is left alone.
"""

import frappe

SEP = " - "


def space_variant_code(doc, method=None):
	"""Item before_insert: runs before the name is set from item_code."""
	if not doc.variant_of or not doc.get("attributes"):
		return
	from erpnext.controllers.item_variant import get_variant_abbreviations

	template = frappe.db.get_value("Item", doc.variant_of, ["item_code", "item_name"], as_dict=True)
	abbreviations = [a for a in get_variant_abbreviations(doc.attributes) if a]
	if not template or not abbreviations:
		return

	def erpnext_default(base):
		return "{}-{}".format(base, "-".join(abbreviations))

	def spaced(base):
		return SEP.join([base, *abbreviations])

	if doc.item_code and doc.item_code == erpnext_default(template.item_code):
		doc.item_code = spaced(template.item_code)
	if doc.item_name and doc.item_name == erpnext_default(template.item_name):
		doc.item_name = spaced(template.item_name)
