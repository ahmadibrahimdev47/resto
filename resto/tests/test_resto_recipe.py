"""Run on a TEST site only (it creates items, BOMs and bundles):

bench --site test.resto run-tests --module resto.tests.test_resto_recipe
"""

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto.doctype.resto_recipe.resto_recipe import MENU, PREPARED
from resto.tests import fixtures as fx


def make_item(code, is_stock=1, uom="Kg", valuation_rate=0):
	if frappe.db.exists("Item", code):
		return frappe.get_doc("Item", code)
	return frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "All Item Groups",
			"stock_uom": uom,
			"is_stock_item": is_stock,
			"include_item_in_manufacturing": 1,
			"valuation_rate": valuation_rate,
		}
	).insert()


def make_recipe(item, recipe_type, items, sides=None, quantity=1):
	doc = frappe.new_doc("Resto Recipe")
	doc.update({"recipe_type": recipe_type, "item": item, "quantity": quantity})
	for code, qty, *uom in items:
		doc.append("items", {"item_code": code, "qty": qty, "uom": uom[0] if uom else None})
	for code, qty in sides or []:
		doc.append("sides", {"item_code": code, "qty": qty})
	return doc.insert()


class TestRestoRecipe(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		settings = frappe.get_doc("Resto Settings")
		settings.company = fx.COMPANY
		settings.prepared_allow_negative = 1
		settings.save()

		make_item("RT-Meat", valuation_rate=400)
		make_item("RT-Onion", valuation_rate=20)
		make_item("RT-Rice", valuation_rate=50)
		make_item("RT-Bread", uom="Nos", valuation_rate=2)
		make_item("RT-Kofta", valuation_rate=0)
		make_item("RT-Quarter-Kofta", is_stock=0, uom="Nos")
		make_item("RT-Mixed-Grill", is_stock=0, uom="Nos")
		make_item("RT-Napkin-Service", is_stock=0, uom="Nos")
		make_item("RT-Combo", is_stock=0, uom="Nos")

	def test_prepared_recipe_creates_default_bom_once(self):
		recipe = make_recipe("RT-Kofta", PREPARED, [("RT-Meat", 0.9), ("RT-Onion", 0.1)])
		self.assertTrue(recipe.bom)
		self.assertEqual(frappe.db.get_value("Item", "RT-Kofta", "default_bom"), recipe.bom)
		self.assertEqual(frappe.db.get_value("Item", "RT-Kofta", "allow_negative_stock"), 1)
		self.assertAlmostEqual(recipe.cost_per_unit, 0.9 * 400 + 0.1 * 20)

		first_bom = recipe.bom
		recipe.preparation = "mix"
		recipe.save()
		self.assertEqual(recipe.bom, first_bom, "no new BOM when ingredients did not change")

		recipe.items[0].qty = 0.8
		recipe.save()
		recipe.reload()
		self.assertNotEqual(recipe.bom, first_bom)
		self.assertEqual(frappe.db.get_value("Item", "RT-Kofta", "default_bom"), recipe.bom)

	def test_menu_dish_flattens_sub_dish_without_its_sides(self):
		if not frappe.db.exists("Resto Recipe", "RT-Kofta"):
			make_recipe("RT-Kofta", PREPARED, [("RT-Meat", 0.9), ("RT-Onion", 0.1)])

		make_recipe(
			"RT-Quarter-Kofta", MENU, [("RT-Kofta", 0.25)], sides=[("RT-Rice", 0.2), ("RT-Bread", 2)]
		)
		mixed = make_recipe(
			"RT-Mixed-Grill",
			MENU,
			[("RT-Quarter-Kofta", 2)],
			sides=[("RT-Rice", 0.3), ("RT-Bread", 4)],
		)
		bundle = frappe.get_doc("Product Bundle", "RT-Mixed-Grill")
		got = {r.item_code: r.qty for r in bundle.items}
		self.assertEqual(got, {"RT-Kofta": 0.5, "RT-Rice": 0.3, "RT-Bread": 4})
		self.assertEqual(mixed.product_bundle, "RT-Mixed-Grill")

	def test_menu_dish_rejects_non_stock_component(self):
		doc = frappe.new_doc("Resto Recipe")
		doc.update({"recipe_type": MENU, "item": "RT-Combo"})
		doc.append("items", {"item_code": "RT-Napkin-Service", "qty": 1})
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_prepared_rejects_non_stock_item(self):
		doc = frappe.new_doc("Resto Recipe")
		doc.update({"recipe_type": PREPARED, "item": "RT-Napkin-Service"})
		doc.append("items", {"item_code": "RT-Meat", "qty": 1})
		self.assertRaises(frappe.ValidationError, doc.insert)
