"""The items page's list (D-288): read through the user's Item rights, each item with its selling
price on the floor's price list (the one valid today, not a customer's own), and its filters.

bench --site test.resto run-tests --module resto.tests.test_items_page
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from resto.resto import items
from resto.tests import fixtures as fx


class TestItemsPage(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self.plist = items.price_list()
		self.assertTrue(self.plist)
		self.code = "RESTO-ITEMS-PAGE-T"
		fx.item(self.code, "Nos")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def price(self, rate, **kw):
		frappe.get_doc(
			{"doctype": "Item Price", "item_code": self.code, "price_list": self.plist, "price_list_rate": rate, **kw}
		).insert(ignore_permissions=True)

	def row(self, **filters):
		got = items.get_items(search=self.code, **filters)["items"]
		return next((r for r in got if r.name == self.code), None)

	def test_the_price_valid_today(self):
		self.assertIsNone(self.row().price)  # none yet: the page shows a dash
		self.price(50, valid_from=add_days(nowdate(), -10))
		self.price(70, valid_from=add_days(nowdate(), 5))  # not yet
		customer = frappe.db.get_value("Customer", {}, "name")
		if customer:
			self.price(10, customer=customer)  # one customer's own
		self.assertEqual(self.row().price, 50)

	def test_filters(self):
		self.assertTrue(self.row(status="active"))
		frappe.db.set_value("Item", self.code, "disabled", 1)
		self.assertIsNone(self.row(status="active"))
		self.assertTrue(self.row(status="disabled"))
		frappe.db.set_value("Item", self.code, "is_purchase_item", 0)
		self.assertIsNone(self.row(status="", tag="purchase"))
		self.assertTrue(self.row(status="", tag="stock"))

	def test_only_with_item_read(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, items.get_items)
		self.assertRaises(frappe.PermissionError, items.get_groups)
