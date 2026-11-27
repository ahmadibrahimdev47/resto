"""A Resto Manager with no manufacturing role submits the daily prep (D-279): its Manufacture entry is
made by the prepared item's BOM, which ERPNext lets only a user who can read the BOM use.

bench --site test.resto run-tests --module resto.tests.test_prep_rights
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from resto.setup import install
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import MANAGER_USER, _manager


class TestPrepRights(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		install.grant_standard_permissions()  # as a migrate does
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_a_plain_manager_submits_the_prep(self):
		self.assertEqual(
			set(frappe.get_roles(MANAGER_USER)) & {"Manufacturing User", "Manufacturing Manager", "Stock Manager", "System Manager"}, set()
		)
		fx.receive(fx.KITCHEN, [("RAW-BEEF", 1, 300)])
		frappe.set_user(MANAGER_USER)
		self.assertTrue(frappe.has_permission("BOM", "read"))
		self.assertFalse(frappe.has_permission("BOM", "write"))
		recipe = frappe.db.get_value("Resto Recipe", {"item": "PREP-PATTY", "recipe_type": "Prepared Ingredient"}, "name")
		doc = frappe.get_doc(
			{
				"doctype": "Resto Prep",
				"company": fx.COMPANY,
				"posting_date": nowdate(),
				"warehouse": fx.KITCHEN,
				"items": [{"recipe": recipe, "item_code": "PREP-PATTY", "qty": 2}],
			}
		)
		doc.insert()
		doc.submit()
		se = doc.items[0].stock_entry
		self.assertTrue(se)
		self.assertEqual(frappe.db.get_value("Stock Entry", se, ["purpose", "docstatus"]), ("Manufacture", 1))

	def test_a_part_is_made_before_the_item_made_of_it(self):
		"""Listed the other way round, the rows are made ingredient first (D-281)."""
		fx.item("PREP-MIX-T", "Kg", group="Resto Prepared")
		mix = fx.recipe("Prepared Ingredient", "PREP-MIX-T", [("PREP-PATTY", 2)])
		patty = frappe.db.get_value("Resto Recipe", {"item": "PREP-PATTY", "recipe_type": "Prepared Ingredient"}, "name")
		fx.receive(fx.KITCHEN, [("RAW-BEEF", 1, 300)])
		before = fx.bin_qty("PREP-PATTY")
		doc = frappe.get_doc(
			{
				"doctype": "Resto Prep",
				"company": fx.COMPANY,
				"posting_date": nowdate(),
				"warehouse": fx.KITCHEN,
				"items": [{"recipe": mix.name, "item_code": "PREP-MIX-T", "qty": 1}, {"recipe": patty, "item_code": "PREP-PATTY", "qty": 2}],
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.assertEqual([(r.idx, r.item_code) for r in doc.items], [(1, "PREP-PATTY"), (2, "PREP-MIX-T")])
		first, second = (frappe.db.get_value("Stock Entry", r.stock_entry, "creation") for r in doc.items)
		self.assertLess(first, second)
		self.assertAlmostEqual(fx.bin_qty("PREP-PATTY"), before)
		self.assertAlmostEqual(fx.bin_qty("PREP-MIX-T"), 1)
