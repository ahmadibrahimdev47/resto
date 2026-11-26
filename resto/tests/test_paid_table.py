"""A dine-in table paid before its food was served stays taken until it is freed (D-220, D-276).

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

	def sent_order(self):
		order = floor.create_order(order_type="Dine In", table=TABLE, guests=2)
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "a"}, {"item_code": "DISH-FRIES", "qty": 1, "notes": "b"}]))
		floor.send_to_kitchen(order)
		return order

	def pay(self, order):
		bill = floor.get_bill(order)
		floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")
		return order

	def paid_order(self):
		return self.pay(self.sent_order())

	def tickets(self, order):
		return {d.ticket for d in frappe.get_doc("Resto Order", order).items}

	def test_paid_first_the_table_stays_while_they_wait_and_while_they_eat(self):
		"""D-276: ready at the pass is not served; served, the guests are eating: the table stays."""
		order = self.paid_order()
		t = self.table()
		self.assertEqual((t.status, t.paid.name, t.paid.waiting, t.paid.eating), ("paid", order, 2, 0))
		self.assertTrue(frappe.db.get_value("Resto Order", order, "paid_before_served"))
		for ticket in self.tickets(order):
			kitchen.set_ticket_status(ticket, "Ready")
		t = self.table()
		self.assertEqual((t.status, t.paid.waiting, t.paid.eating), ("paid", 2, 0))
		floor.mark_served(order)
		t = self.table()
		self.assertEqual((t.status, t.paid.waiting, t.paid.eating), ("paid", 0, 1))
		floor.free_table(order)
		self.assertEqual(self.table().status, "free")

	def test_ready_when_paid_is_served_and_they_are_eating(self):
		order = self.sent_order()
		for ticket in self.tickets(order):
			kitchen.set_ticket_status(ticket, "Ready")
		self.pay(order)
		self.assertEqual({d.kitchen_status for d in frappe.get_doc("Resto Order", order).items}, {"Served"})
		t = self.table()
		self.assertEqual((t.status, t.paid.eating), ("paid", 1))

	def test_paid_after_eating_the_table_frees_at_once(self):
		order = self.sent_order()
		for ticket in self.tickets(order):
			kitchen.set_ticket_status(ticket, "Ready")
		floor.mark_served(order)
		self.pay(order)
		self.assertFalse(frappe.db.get_value("Resto Order", order, "paid_before_served"))
		self.assertEqual(self.table().status, "free")

	def test_a_dish_left_ready_after_payment_is_served_from_the_floor_or_the_kitchen(self):
		"""D-277: a ticket ready after payment leaves the kitchen board after two hours; the waiter
		serves it from the floor (one line's ticket or all), or the kitchen from Today's Tickets."""
		order = self.paid_order()
		tickets = sorted(self.tickets(order))
		for ticket in tickets:
			kitchen.set_ticket_status(ticket, "Ready")
		t = self.table()
		self.assertEqual((t.paid.waiting, t.paid.ready, t.paid.eating), (2, 2, 0))
		frappe.set_user("waiter@resto.test")
		floor.mark_served(order, tickets[0])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", tickets[0], "status"), "Served")
		# Ready again, Today's Tickets lists it so, and the kitchen serves it there.
		kitchen.set_ticket_status(tickets[0], "Ready")
		listed = {r.name: r.status for r in kitchen.kds_day_tickets()["tickets"]}
		self.assertEqual(listed.get(tickets[0]), "Ready")
		kitchen.kds_set_status(tickets[0], "Served")
		floor.mark_served(order)  # the rest, if the dishes were on more than one ticket
		t = self.table()
		self.assertEqual((t.status, t.paid.waiting, t.paid.ready, t.paid.eating), ("paid", 0, 0, 1))

	def test_the_waiter_frees_it_by_hand(self):
		order = self.paid_order()
		frappe.set_user("waiter@resto.test")
		floor.free_table(order)
		frappe.set_user("Administrator")
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
