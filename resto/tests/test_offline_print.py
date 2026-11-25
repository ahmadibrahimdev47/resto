"""The kitchen ticket printed by a floor screen without a connection (D-219), recorded when it is back.

bench --site test.resto run-tests --module resto.tests.test_offline_print
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from resto.resto import floor
from resto.tests import fixtures as fx


class TestOfflinePrint(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		self._shift = frappe.db.get_single_value("Resto Settings", "require_pos_shift")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", self._shift)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def order_with(self, *notes):
		order = floor.create_order(order_type="Takeaway", customer_name="Offline Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": n} for n in notes]))
		return order

	def lines(self, order):
		return frappe.get_doc("Resto Order", order).items

	def test_only_the_printed_dishes_are_recorded_and_not_printed_again(self):
		# A dish added on another screen while this one was offline; this screen's two dishes reach the
		# server when its connection is back, after it (the newest are the ones it printed).
		order = self.order_with("other screen")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": n} for n in ("one", "two")]))
		view = floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), view=1, op="offlineSend1")
		self.assertEqual(view["name"], order)
		status = [d.kitchen_status for d in self.lines(order)]
		self.assertEqual(status, ["Not Sent", "Sent", "Sent"])
		tickets = {d.ticket for d in self.lines(order) if d.ticket}
		self.assertEqual(len(tickets), 1)
		for t in tickets:
			self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", t, "printed"), 1)

		# Sent again (the answer was lost): nothing more is recorded.
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), view=1, op="offlineSend1")
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Not Sent", "Sent", "Sent"])
		self.assertEqual(frappe.db.count("Resto Kitchen Ticket", {"resto_order": order}), 1)

		# The third goes the usual way, printed by its station.
		floor.send_to_kitchen(order)
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Sent", "Sent", "Sent"])
		last = self.lines(order)[0].ticket
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", last, "printed"), 0)

	def test_a_plain_line_bigger_than_printed_is_split(self):
		# Two printed; the same dish tapped once more meanwhile (the line went to 3).
		order = floor.create_order(order_type="Takeaway", customer_name="Offline Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 3}]))
		before = frappe.db.get_value("Resto Order", order, "net_total")
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), op="offlineSend2")
		self.assertEqual([(d.qty, d.kitchen_status) for d in self.lines(order)], [(2, "Sent"), (1, "Not Sent")])
		self.assertEqual(frappe.db.get_value("Resto Order", order, "net_total"), before, "the bill is unchanged")

	def test_the_lines_the_device_printed_are_matched_first(self):
		# The line this device printed (with a note), then a newer plain line from another screen.
		order = floor.create_order(order_type="Takeaway", customer_name="Offline Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "mine"}]))
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		mine = next(d.name for d in self.lines(order) if d.notes == "mine")
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 1}), op="offlineSend7", lines=json.dumps([mine]))
		self.assertEqual({d.notes or "": d.kitchen_status for d in self.lines(order)}, {"": "Not Sent", "mine": "Sent"})

	def test_the_rest_of_a_split_line_keeps_its_price(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Offline Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 3}]))
		line = self.lines(order)[0]
		frappe.db.set_value("Resto Order Item", line.name, "rate", flt(line.rate) + 7)  # priced before a change
		rate = flt(line.rate) + 7
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), op="offlineSend8")
		self.assertEqual([flt(d.rate) for d in self.lines(order)], [rate, rate])

	def test_a_void_carries_the_number_of_the_ticket_it_cancels(self):
		order = self.order_with("one")
		floor.send_to_kitchen(order)
		line = self.lines(order)[0]
		frappe.db.set_value("Resto Kitchen Ticket", line.ticket, "order_no", 999)  # as an order merged in
		floor.void_line(order, line.name, "Wrong dish")
		void = frappe.get_all("Resto Kitchen Ticket", filters={"resto_order": order, "ticket_type": "Void"}, pluck="order_no")
		self.assertEqual(void, [999])

	def test_printed_but_not_on_the_order_is_noted_for_the_cashier(self):
		order = self.order_with("one")
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), op="offlineSend4")
		notes = frappe.get_all(
			"Comment", filters={"reference_doctype": "Resto Order", "reference_name": order}, pluck="content"
		)
		self.assertTrue(any("1 ×" in n for n in notes), notes)

	def test_recorded_at_the_time_it_was_printed(self):
		order = self.order_with("one")
		# Opened before it was printed (a time before the order opened is not taken).
		frappe.db.set_value("Resto Order", order, "opened_at", add_to_date(now_datetime(), minutes=-30))
		at = add_to_date(now_datetime(), minutes=-5)
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 1}), op="offlineSend5", printed_at=str(at))
		ticket = self.lines(order)[0].ticket
		self.assertEqual(get_datetime(frappe.db.get_value("Resto Kitchen Ticket", ticket, "sent_at")).replace(microsecond=0), at.replace(microsecond=0))

	def test_a_send_that_arrived_and_its_paper_printed_here_are_recorded_once(self):
		order = self.order_with("one")
		floor.send_to_kitchen(order, op="onlineSend6")  # its answer was lost
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 1}), op="onlineSend6")
		self.assertEqual(frappe.db.count("Resto Kitchen Ticket", {"resto_order": order}), 1)
		ticket = self.lines(order)[0].ticket
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", ticket, "printed"), 0)

	def test_a_bad_printed_value_records_nothing(self):
		order = self.order_with("one")
		self.assertEqual(floor.send_to_kitchen(order, printed=json.dumps(["DISH-FRIES"]), op="offlineSend3"), [])
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Not Sent"])

	def test_the_floor_reads_the_bill_rules(self):
		rules = floor.get_floor()["bill_rules"]
		self.assertIn("taxes", rules)
		self.assertIn("disable_rounded_total", rules)
		self.assertIn("smallest_fraction", rules)
		self.assertIn("rounding_method", rules)
		self.assertIn(rules["exact"], (0, 1))
