"""«Served, waiting for payment» on the floor (D-291): a dine-in table whose every dish was served
shows it by itself; the floor can mark it by hand, asked first when dishes are not ready; adding a
dish takes it back to occupied; a bill asked for shows first.

bench --site test.resto run-tests --module resto.tests.test_floor_served
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import floor, kitchen
from resto.tests import fixtures as fx

TABLE = "T5"


class TestFloorServed(IntegrationTestCase):
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

	def sent_order(self):
		order = floor.create_order(order_type="Dine In", table=TABLE, guests=2)
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}, {"item_code": "DRK-COLA", "qty": 2}]))
		floor.send_to_kitchen(order)
		return order

	def tickets(self, order):
		return sorted({d.ticket for d in frappe.get_doc("Resto Order", order).items if d.ticket})

	def serve_all(self, order):
		for ticket in self.tickets(order):
			kitchen.set_ticket_status(ticket, "Ready")
			kitchen.set_ticket_status(ticket, "Served")

	def test_all_served_shows_by_itself_and_a_new_dish_clears_it(self):
		order = self.sent_order()
		self.assertEqual(self.table().status, "occupied")
		self.serve_all(order)
		t = self.table()
		self.assertEqual((t.status, t.order.waiting_payment), ("served", 1))
		self.assertGreaterEqual(t.order.served_minutes, 0)

		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "more"}]))
		t = self.table()
		self.assertEqual((t.status, t.order.waiting_payment), ("occupied", 0))

	def test_marked_by_hand_asks_about_dishes_not_ready(self):
		order = self.sent_order()
		first, *rest = self.tickets(order)
		self.assertTrue(rest)  # the fries and the drinks go to two stations
		kitchen.set_ticket_status(first, "Ready")

		asked = floor.mark_waiting_payment(order)
		self.assertEqual(asked["done"], 0)
		self.assertGreater(asked["not_ready"], 0)
		self.assertEqual(self.table().status, "occupied")  # nothing changed yet
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", first, "status"), "Ready")

		done = floor.mark_waiting_payment(order, confirm=1)
		self.assertEqual(done["done"], 1)
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", first, "status"), "Served")  # what was ready is served
		self.assertTrue(frappe.db.get_value("Resto Order", order, "served_marked_at"))
		self.assertEqual(self.table().status, "served")

		floor.add_items(order, json.dumps([{"item_code": "DRK-COLA", "qty": 1, "notes": "one more"}]))
		self.assertFalse(frappe.db.get_value("Resto Order", order, "served_marked_at"))
		self.assertEqual(self.table().status, "occupied")

	def test_all_ready_is_marked_without_asking(self):
		order = self.sent_order()
		for ticket in self.tickets(order):
			kitchen.set_ticket_status(ticket, "Ready")
		self.assertEqual(floor.mark_waiting_payment(order), {"done": 1, "not_ready": 0})
		self.assertEqual({d.kitchen_status for d in frappe.get_doc("Resto Order", order).items}, {"Served"})
		self.assertEqual(self.table().status, "served")

	def test_a_bill_asked_for_shows_first(self):
		order = self.sent_order()
		self.serve_all(order)
		self.assertEqual(self.table().status, "served")
		floor.request_bill(order)
		t = self.table()
		self.assertEqual(t.status, "bill")
		self.assertEqual(t.order.waiting_payment, 1)  # still served: once the bill is changed, served shows again
