"""«Served, waiting for payment» on the floor (D-291): a dine-in table whose every dish was served
shows it by itself; adding a dish takes it back to occupied; a bill asked for shows first. The
floor's kitchen state buttons (D-293) set any state at once, back only when confirmed, alongside the
kitchen screen, and their times stay out of the kitchen times.

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

	def test_a_bill_asked_for_shows_first(self):
		order = self.sent_order()
		self.serve_all(order)
		self.assertEqual(self.table().status, "served")
		floor.request_bill(order)
		t = self.table()
		self.assertEqual(t.status, "bill")
		self.assertEqual(t.order.waiting_payment, 1)  # still served: once the bill is changed, served shows again

	# ------------------------------------------------------------------ the floor's kitchen state (D-293)

	def ticket(self, name):
		return frappe.db.get_value(
			"Resto Kitchen Ticket", name, ["status", "ready_at", "served_at", "floor_started", "floor_ready", "floor_served"], as_dict=True
		)

	def line_states(self, order):
		return {d.kitchen_status for d in frappe.get_doc("Resto Order", order).items if d.kitchen_status != "Void"}

	def test_floor_jumps_straight_to_served_and_flags_it(self):
		order = self.sent_order()
		self.assertEqual(floor.set_kitchen_state(order, "Served")["done"], 1)  # from the kitchen, no steps between
		self.assertEqual(self.line_states(order), {"Served"})
		self.assertEqual(self.table().status, "served")
		for name in self.tickets(order):
			t = self.ticket(name)
			self.assertEqual((t.status, t.floor_ready, t.floor_served), ("Served", 1, 1))
			self.assertTrue(t.ready_at and t.served_at)

	def test_floor_goes_back_only_when_confirmed(self):
		order = self.sent_order()
		floor.set_kitchen_state(order, "Served")
		asked = floor.set_kitchen_state(order, "Sent")
		self.assertEqual((asked["done"], asked["back"]), (0, len(self.tickets(order))))
		self.assertEqual(self.line_states(order), {"Served"})  # nothing moved yet
		floor.set_kitchen_state(order, "Sent", confirm=1)
		self.assertEqual(self.line_states(order), {"Sent"})
		self.assertFalse(frappe.db.get_value("Resto Order", order, "served_marked_at"))
		self.assertEqual(self.table().status, "occupied")
		for name in self.tickets(order):
			t = self.ticket(name)
			self.assertEqual((t.status, t.ready_at, t.served_at, t.floor_ready, t.floor_served), ("New", None, None, 0, 0))
		# Forward is never asked about.
		self.assertEqual(floor.set_kitchen_state(order, "Ready")["done"], 1)

	def test_kitchen_and_floor_both_move_tickets_and_only_the_floor_is_flagged(self):
		order = self.sent_order()
		first, second = self.tickets(order)[:2]
		# The kitchen screen marks one ready, then the floor serves the table.
		kitchen.set_ticket_status(first, "Ready")
		floor.set_kitchen_state(order, "Served")
		t = self.ticket(first)
		self.assertEqual((t.status, t.floor_ready, t.floor_served), ("Served", 0, 1))
		# The floor takes it back to ready, then the kitchen screen serves it: that step is the kitchen's.
		floor.set_kitchen_state(order, "Ready", confirm=1)
		kitchen.set_ticket_status(second, "Served")
		t = self.ticket(second)
		self.assertEqual((t.status, t.floor_ready, t.floor_served), ("Served", 1, 0))  # the ready time stays the floor's
		self.assertEqual(
			frappe.db.get_value("Resto Order Item", {"ticket": second, "kitchen_status": ("!=", "Void")}, "kitchen_status"), "Served"
		)

	def test_floor_set_times_are_left_out_of_the_kitchen_times(self):
		from frappe.utils import now_datetime

		from resto.resto.report.resto_kitchen_times.resto_kitchen_times import spans

		order = self.sent_order()
		floor.set_kitchen_state(order, "Served")
		name = self.tickets(order)[0]
		t = frappe.db.get_value(
			"Resto Kitchen Ticket", name,
			["sent_at", "started_at", "ready_at", "served_at", "floor_started", "floor_ready", "floor_served"], as_dict=True,
		)
		self.assertEqual(spans(t, now_datetime(), 20), (None, None, None, None, None))  # no 0-minute prep, lateness not known
		frappe.db.set_value("Resto Kitchen Ticket", name, {"floor_ready": 0, "floor_served": 0})  # as if the kitchen had
		t.floor_ready = t.floor_served = 0
		wait, cook, total, pickup, late = spans(t, now_datetime(), 20)
		self.assertIsNotNone(total)

	# ------------------------------------------------------------------ a stale screen (D-295)

	def test_the_floor_on_a_stale_view_does_not_move_what_the_kitchen_moved(self):
		order = self.sent_order()
		for name in self.tickets(order):
			kitchen.kds_set_status(name, "Ready")  # the kitchen screen
		# The floor still showed «in the kitchen» and taps Served.
		r = floor.set_kitchen_state(order, "Served", seen="Sent")
		self.assertEqual((r["done"], r["changed"]), (0, "Ready"))
		self.assertIn("the kitchen", r["message"])
		self.assertEqual(self.line_states(order), {"Ready"})  # nothing moved
		self.assertFalse(frappe.db.get_value("Resto Order", order, "served_marked_at"))
		# Looked again, it taps on what is true.
		self.assertEqual(floor.set_kitchen_state(order, "Served", seen="Ready")["done"], 1)
		self.assertEqual(self.line_states(order), {"Served"})
		# Tapped twice on the same state from two screens: no complaint.
		self.assertEqual(floor.set_kitchen_state(order, "Served", seen="Ready")["done"], 1)

	def test_the_kitchen_on_a_stale_view_does_not_move_what_the_floor_moved(self):
		order = self.sent_order()
		name = self.tickets(order)[0]
		floor.set_kitchen_state(order, "Served", seen="Sent")  # the floor
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", name, "moved_from"), "Floor")
		# The kitchen screen still showed it new and taps Ready.
		with self.assertRaises(kitchen.StaleTicketState) as e:
			kitchen.kds_set_status(name, "Ready", seen="New")
		self.assertIn("the floor", str(e.exception))
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", name, "status"), "Served")
		# Looked again: moved from what it shows, it moves.
		kitchen.kds_set_status(name, "Ready", seen="Served")
		self.assertEqual(
			frappe.db.get_value("Resto Kitchen Ticket", name, ["status", "moved_from"]), ("Ready", "Kitchen")
		)

	def test_a_new_dish_after_served_from_the_floor_takes_the_table_back(self):
		order = self.sent_order()
		floor.set_kitchen_state(order, "Served")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "after"}]))
		self.assertFalse(frappe.db.get_value("Resto Order", order, "served_marked_at"))
		self.assertEqual(self.table().status, "occupied")
