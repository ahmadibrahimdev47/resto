import frappe

from resto.resto.food_cost import DEFAULTS, FIELDS


def execute():
	"""Fill the new dish food cost levels on sites that already have Resto Settings."""
	for key, field in FIELDS.items():
		if not frappe.db.get_single_value("Resto Settings", field):
			frappe.db.set_single_value("Resto Settings", field, DEFAULTS[key])
