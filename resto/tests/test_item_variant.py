"""Variant codes with spaced dashes. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_item_variant
"""

import frappe
from erpnext.controllers.item_variant import create_variant
from frappe.tests import IntegrationTestCase

from resto.tests import fixtures as fx


class TestItemVariant(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.tag = frappe.generate_hash(length=5)
		cls.size = f"RTV Size {cls.tag}"
		cls.spice = f"RTV Spice {cls.tag}"
		for name, values in ((cls.size, [("Quarter", "1/4"), ("Half", "1/2")]), (cls.spice, [("Hot", "HOT")])):
			frappe.get_doc(
				{
					"doctype": "Item Attribute",
					"attribute_name": name,
					"item_attribute_values": [{"attribute_value": v, "abbr": a} for v, a in values],
				}
			).insert()

	def template(self, *attributes):
		code = f"RTV Kofta {frappe.generate_hash(length=5)}"
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"has_variants": 1,
				"attributes": [{"attribute": a} for a in attributes],
			}
		).insert()

	def test_variant_code_and_name_get_spaced_dashes(self):
		t = self.template(self.size)
		v = create_variant(t.name, {self.size: "Half"})
		self.assertEqual(v.item_code, f"{t.name}-1/2")  # ERPNext's own output
		v.insert()
		self.assertEqual(v.name, f"{t.name} - 1/2")
		self.assertEqual(v.item_code, f"{t.name} - 1/2")
		self.assertEqual(v.item_name, f"{t.item_name} - 1/2")

	def test_several_attributes(self):
		t = self.template(self.size, self.spice)
		v = create_variant(t.name, {self.size: "Quarter", self.spice: "Hot"})
		v.insert()
		self.assertEqual(v.name, f"{t.name} - 1/4 - HOT")

	def test_typed_code_is_left_alone(self):
		t = self.template(self.size)
		v = create_variant(t.name, {self.size: "Quarter"})
		v.item_code = f"{t.name} small"
		v.item_name = "Small kofta"
		v.insert()
		self.assertEqual(v.name, f"{t.name} small")
		self.assertEqual(v.item_name, "Small kofta")
