"""A dine-in table paid while its food is still in the kitchen stays taken (D-220).

bench --site test.resto run-tests --module resto.tests.test_paid_table
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import floor, kitchen
from resto.tests import fixtures as fx

TABLE = "T6"


class TestPaidTable(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		self._shift = frappe.db.get_single_value("Resto Settings", "require_pos_shift")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		for name in frappe.get_all("Resto Order", filters={"resto_table": TABLE, "status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")
		for name in frappe.get_all("Resto Order", filters={"resto_table": TABLE, "status": "Paid"}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "table_freed", 1)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", self._shift)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def table(self):
		return next(t for h in floor.get_floor()["halls"] for t in h["tables"] if t.name == TABLE)

	def paid_order(self):
		order = floor.create_order(order_type="Dine In", table=TABLE, guests=2)
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "a"}, {"item_code": "DISH-FRIES", "qty": 1, "notes": "b"}]))
		floor.send_to_kitchen(order)
		bill = floor.get_bill(order)
		floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")
		return order

	def test_the_table_waits_for_the_food_then_frees_by_itself(self):
		order = self.paid_order()
		t = self.table()
		self.assertEqual(t.status, "paid")
		self.assertEqual(t.paid.name, order)
		self.assertEqual(t.paid.waiting, 2)

		tickets = {d.ticket for d in frappe.get_doc("Resto Order", order).items}
		for ticket in tickets:
			kitchen.set_ticket_status(ticket, "Ready")
		self.assertEqual(self.table().status, "free")

	def test_the_cashier_frees_it_by_hand(self):
		order = self.paid_order()
		floor.free_table(order)
		self.assertEqual(self.table().status, "free")

	def test_only_a_paid_dine_in_order_is_freed_here(self):
		order = floor.create_order(order_type="Dine In", table=TABLE, guests=2)
		self.assertRaises(frappe.ValidationError, floor.free_table, order)

	def test_takeaway_is_unchanged(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Paid Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		bill = floor.get_bill(order)
		floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))
		self.assertEqual(self.table().status, "free")
