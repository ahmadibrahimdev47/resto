"""The items page's list (D-288): read through the user's Item rights, each item with its selling
price on the floor's price list (the one valid today, not a customer's own), and its filters. The
manager sets an item's reorder level there without the right to change the item (D-290).

bench --site test.resto run-tests --module resto.tests.test_items_page
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from resto.resto import items
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import MANAGER_USER, WAITER_USER, _manager


class TestItemsPage(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
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

	def reorder_rows(self):
		return frappe.get_all(
			"Item Reorder",
			filters={"parent": self.code, "parenttype": "Item"},
			fields=["warehouse", "warehouse_reorder_level", "warehouse_reorder_qty", "material_request_type"],
		)

	def test_manager_sets_the_reorder_level_and_nothing_else(self):
		fields = ("item_name", "item_group", "stock_uom", "description", "is_sales_item", "is_purchase_item", "disabled")
		before = frappe.db.get_value("Item", self.code, fields, as_dict=True)
		frappe.set_user(MANAGER_USER)
		self.assertFalse(frappe.has_permission("Item", "write"))  # the manager may not change items
		items.set_reorder(self.code, fx.KITCHEN, level=5, qty=10)
		items.set_reorder(self.code, fx.KITCHEN, level=4, qty=12, material_request_type="Transfer")  # the same row
		rows = self.reorder_rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].warehouse, flt(rows[0].warehouse_reorder_level), flt(rows[0].warehouse_reorder_qty), rows[0].material_request_type),
			(fx.KITCHEN, 4, 12, "Transfer"),
		)
		got = items.get_items(search=self.code)
		self.assertTrue(got["can_reorder"])
		card = next(r for r in got["items"] if r.name == self.code)
		self.assertEqual((card.reorder.warehouse, card.reorder.level), (fx.KITCHEN, 4))
		self.assertEqual(frappe.db.get_value("Item", self.code, fields, as_dict=True), before)

		self.assertRaises(frappe.ValidationError, items.set_reorder, self.code, fx.KITCHEN, level=5, qty=0)
		self.assertRaises(frappe.ValidationError, items.set_reorder, self.code, fx.KITCHEN, level=-1, qty=3)
		self.assertRaises(frappe.ValidationError, items.set_reorder, self.code, fx.KITCHEN, level=1, qty=3, material_request_type="Gift")

		items.set_reorder(self.code, fx.KITCHEN, remove=1)
		self.assertEqual(self.reorder_rows(), [])

	def test_reorder_level_needs_the_manager(self):
		frappe.set_user(WAITER_USER)
		self.assertRaises(frappe.PermissionError, items.set_reorder, self.code, fx.KITCHEN, level=5, qty=10)
		frappe.set_user("Administrator")
		self.assertEqual(self.reorder_rows(), [])
