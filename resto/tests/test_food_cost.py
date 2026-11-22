"""Dish food cost levels and the save warning. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_food_cost
"""

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import food_cost


class TestFoodCost(IntegrationTestCase):
	LEVELS = {"green": 50, "yellow": 65, "orange": 80}

	def test_levels(self):
		cases = {0: "green", 50: "green", 50.01: "yellow", 65: "yellow", 70: "orange", 80: "orange",
			80.5: "red", 99.99: "red", 100: "over", 234: "over"}
		for percent, level in cases.items():
			self.assertEqual(food_cost.get_level(percent, self.LEVELS), level, percent)

	def test_levels_out_of_order_fall_back_to_defaults(self):
		settings = frappe.get_doc("Resto Settings")
		before = {f: settings.get(f) for f in food_cost.FIELDS.values()}
		try:
			for field, value in zip(food_cost.FIELDS.values(), (70, 65, 80)):
				frappe.db.set_single_value("Resto Settings", field, value)
			self.assertEqual(food_cost.get_levels(), food_cost.DEFAULTS)
		finally:
			for field, value in before.items():
				frappe.db.set_single_value("Resto Settings", field, value)

	def test_settings_reject_levels_out_of_order(self):
		settings = frappe.get_doc("Resto Settings")
		settings.food_cost_green_upto, settings.food_cost_yellow_upto, settings.food_cost_orange_upto = 70, 65, 80
		self.assertRaises(frappe.ValidationError, settings.validate_food_cost_levels)
		settings.food_cost_green_upto, settings.food_cost_yellow_upto, settings.food_cost_orange_upto = 0, 0, 0
		settings.validate_food_cost_levels()  # empty levels take the defaults
		self.assertEqual(settings.food_cost_yellow_upto, 65)

	def warnings(self, percent):
		frappe.clear_messages()
		food_cost.warn(percent)
		return [frappe.parse_json(m) for m in frappe.local.message_log]

	def test_warning_only_above_green_and_never_blocks(self):
		self.assertEqual(self.warnings(40), [])
		self.assertEqual(len(self.warnings(60)), 1)
		over = self.warnings(234)
		self.assertEqual(len(over), 1)
		self.assertIn("234", over[0]["message"])
		self.assertEqual(over[0]["indicator"], "red")
		frappe.clear_messages()
