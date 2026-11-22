import frappe

from resto.resto.food_cost import STEP_FIELDS, STEPS


def execute():
	"""Fill the food cost color steps on sites that already have Resto Settings."""
	for key, field in STEP_FIELDS.items():
		if not frappe.db.get_single_value("Resto Settings", field):
			frappe.db.set_single_value("Resto Settings", field, STEPS[key])
