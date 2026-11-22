"""Recipe tab on Item. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_item_recipe
"""

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import item_recipe
from resto.resto.doctype.resto_recipe.resto_recipe import PREPARED
from resto.tests import fixtures as fx

P = "resto_recipe_"


class TestItemRecipe(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		fx.item("RTI-Meat", uom="Kg", valuation=400, sales=0)
		fx.item("RTI-Onion", uom="Kg", valuation=20, sales=0)

	def setUp(self):
		frappe.set_user("Administrator")
		self.code = "RTI-Kofta-" + frappe.generate_hash(length=6)

	def new_item(self, **tab):
		values = {
			"doctype": "Item",
			"item_code": self.code,
			"item_name": self.code,
			"item_group": "All Item Groups",
			"stock_uom": "Kg",
			"is_stock_item": 1,
			P + "type": PREPARED,
			P + "quantity": 10,
			P + "items": [{"item_code": "RTI-Meat", "qty": 9}, {"item_code": "RTI-Onion", "qty": 1}],
		}
		values.update(tab)
		return frappe.get_doc(values).insert()

	def stored_rows_on_item(self):
		return frappe.db.count("Resto Recipe Item", {"parenttype": "Item", "parent": self.code})

	def test_new_item_creates_its_recipe(self):
		item = self.new_item()
		recipe = frappe.get_doc("Resto Recipe", self.code)
		self.assertEqual(recipe.recipe_type, PREPARED)
		self.assertEqual(recipe.quantity, 10)
		self.assertEqual([(r.item_code, r.qty) for r in recipe.items], [("RTI-Meat", 9), ("RTI-Onion", 1)])
		self.assertAlmostEqual(recipe.cost_per_unit, (9 * 400 + 1 * 20) / 10)
		self.assertTrue(recipe.bom)
		# Nothing of the recipe is stored on the Item itself.
		self.assertEqual(self.stored_rows_on_item(), 0)
		self.assertFalse(frappe.db.get_value("Item", self.code, P + "type"))
		# The saved form shows the recipe again.
		self.assertEqual(len(item.get(P + "items")), 2)
		self.assertAlmostEqual(item.get(P + "cost_per_unit"), recipe.cost_per_unit)

	def test_untouched_tab_does_not_rewrite_the_recipe(self):
		self.new_item()
		before = frappe.db.get_value("Resto Recipe", self.code, "modified")
		item = frappe.get_doc("Item", self.code)
		item.run_method("onload")
		self.assertEqual(len(item.get(P + "items")), 2)
		item.description = "changed"
		item.save()
		self.assertEqual(frappe.db.get_value("Resto Recipe", self.code, "modified"), before)
		self.assertEqual(self.stored_rows_on_item(), 0)

	def test_edit_from_the_item_updates_the_recipe(self):
		self.new_item()
		item = frappe.get_doc("Item", self.code)
		item.run_method("onload")
		item.get(P + "items")[0].qty = 8
		item.set(P + "changed", 1)
		item.save()
		recipe = frappe.get_doc("Resto Recipe", self.code)
		self.assertEqual(recipe.items[0].qty, 8)
		self.assertEqual(self.stored_rows_on_item(), 0)

	def test_stale_tab_is_refused(self):
		self.new_item()
		item = frappe.get_doc("Item", self.code)
		item.run_method("onload")
		recipe = frappe.get_doc("Resto Recipe", self.code)
		recipe.par_qty = 3
		recipe.save()
		item.get(P + "items")[0].qty = 7
		item.set(P + "changed", 1)
		self.assertRaises(frappe.ValidationError, item.save)

	def test_item_saved_by_code_leaves_recipe_alone(self):
		self.new_item()
		before = frappe.db.get_value("Resto Recipe", self.code, "modified")
		item = frappe.get_doc("Item", self.code)  # no onload: the tab is empty
		item.description = "by code"
		item.save()
		self.assertEqual(frappe.db.get_value("Resto Recipe", self.code, "modified"), before)
		self.assertTrue(frappe.db.exists("Resto Recipe", self.code))

	def test_fields_are_defined(self):
		names = {f["fieldname"] for f in item_recipe.ITEM_FIELDS["Item"]}
		for f in (P + "type", P + "items", P + "sides", P + "changed", P + "modified"):
			self.assertIn(f, names)
		self.assertTrue(frappe.get_meta("Item").has_field(P + "items"))

	def test_recipe_status_and_flag(self):
		from resto.resto.doctype.resto_recipe.resto_recipe import recipe_status

		self.new_item()
		self.assertEqual(frappe.db.get_value("Item", self.code, "resto_needs_recipe"), 1)
		bare = fx.item("RTI-Bare-" + frappe.generate_hash(length=6), stock=0)
		frappe.db.set_value("Item", bare.name if hasattr(bare, "name") else bare, "resto_needs_recipe", 1)
		rows = {r["item"]: r for r in recipe_status()["rows"]}
		self.assertEqual(rows[self.code]["status"], "Active")
		bare_name = bare.name if hasattr(bare, "name") else bare
		self.assertEqual(rows[bare_name]["status"], "Missing")
		self.assertEqual(rows[bare_name]["expected_type"], "Menu Dish")

	def test_dish_target_goes_through_the_tab(self):
		from resto.resto.doctype.resto_recipe.resto_recipe import MENU

		self.new_item(**{
			"stock_uom": "Nos",
			"is_stock_item": 0,
			P + "type": MENU,
			P + "quantity": 1,
			P + "items": [{"item_code": "RTI-Meat", "qty": 0.25}],
			P + "target_food_cost_percent": 40,
		})
		self.assertEqual(frappe.db.get_value("Resto Recipe", self.code, "target_food_cost_percent"), 40)
		item = frappe.get_doc("Item", self.code)
		item.run_method("onload")
		self.assertEqual(item.get(P + "target_food_cost_percent"), 40)
		item.set(P + "target_food_cost_percent", 55)
		item.set(P + "changed", 1)
		item.save()
		self.assertEqual(frappe.db.get_value("Resto Recipe", self.code, "target_food_cost_percent"), 55)
		self.assertFalse(frappe.db.get_value("Item", self.code, P + "target_food_cost_percent"))
