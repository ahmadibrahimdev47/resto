"""Dish food cost levels and the save warning. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_food_cost
"""

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import food_cost

FIELDS = (food_cost.TARGET_FIELD, *food_cost.STEP_FIELDS.values())


class TestFoodCost(IntegrationTestCase):
	def setUp(self):
		self.before = {f: frappe.db.get_single_value("Resto Settings", f) for f in FIELDS}
		self.set_levels(50, 15, 30)

	def tearDown(self):
		for field, value in self.before.items():
			frappe.db.set_single_value("Resto Settings", field, value)
		frappe.clear_messages()

	def set_levels(self, target, yellow, orange):
		for field, value in zip(FIELDS, (target, yellow, orange)):
			frappe.db.set_single_value("Resto Settings", field, value)

	def test_levels_follow_the_target(self):
		self.assertEqual(food_cost.get_levels(), {"green": 50, "yellow": 65, "orange": 80})
		cases = {0: "green", 50: "green", 50.01: "yellow", 65: "yellow", 70: "orange", 80: "orange",
			80.5: "red", 99.99: "red", 100: "over", 234: "over"}
		for percent, level in cases.items():
			self.assertEqual(food_cost.get_level(percent), level, percent)

	def test_high_target_still_marks_over_100(self):
		self.set_levels(80, 15, 30)
		self.assertEqual(food_cost.get_level(95), "yellow")
		self.assertEqual(food_cost.get_level(100), "over")

	def test_steps_out_of_order_fall_back_to_defaults(self):
		self.set_levels(35, 30, 15)
		self.assertEqual(food_cost.get_levels(), {"green": 35, "yellow": 50, "orange": 65})

	def test_settings_reject_steps_out_of_order(self):
		settings = frappe.get_doc("Resto Settings")
		settings.food_cost_yellow_step, settings.food_cost_orange_step = 30, 15
		self.assertRaises(frappe.ValidationError, settings.validate_food_cost_levels)
		settings.food_cost_yellow_step, settings.food_cost_orange_step = 0, 0
		settings.validate_food_cost_levels()  # empty steps take the defaults
		self.assertEqual((settings.food_cost_yellow_step, settings.food_cost_orange_step), (15, 30))

	def warnings(self, percent):
		frappe.clear_messages()
		food_cost.warn(percent)
		return [frappe.parse_json(m) for m in frappe.local.message_log]

	def test_warning_only_above_target_and_never_blocks(self):
		self.assertEqual(self.warnings(50), [])
		self.assertEqual(self.warnings(60)[0]["indicator"], "yellow")
		over = self.warnings(234)
		self.assertEqual(len(over), 1)
		self.assertIn("234", over[0]["message"])
		self.assertEqual(over[0]["indicator"], "red")
