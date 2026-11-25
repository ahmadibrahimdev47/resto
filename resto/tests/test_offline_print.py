"""The kitchen ticket printed by a floor screen without a connection (D-219), recorded when it is back.

bench --site test.resto run-tests --module resto.tests.test_offline_print
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

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
		# Two dishes printed on the screen; a third added meanwhile on another screen.
		order = self.order_with("one", "two", "other screen")
		view = floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), view=1, op="offlineSend1")
		self.assertEqual(view["name"], order)
		status = [d.kitchen_status for d in self.lines(order)]
		self.assertEqual(status, ["Sent", "Sent", "Not Sent"])
		tickets = {d.ticket for d in self.lines(order) if d.ticket}
		self.assertEqual(len(tickets), 1)
		for t in tickets:
			self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", t, "printed"), 1)

		# Sent again (the answer was lost): nothing more is recorded.
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), view=1, op="offlineSend1")
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Sent", "Sent", "Not Sent"])
		self.assertEqual(frappe.db.count("Resto Kitchen Ticket", {"resto_order": order}), 1)

		# The third goes the usual way, printed by its station.
		floor.send_to_kitchen(order)
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Sent", "Sent", "Sent"])
		last = self.lines(order)[2].ticket
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", last, "printed"), 0)

	def test_a_bigger_line_than_printed_is_left_unsent(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Offline Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 3}]))
		floor.send_to_kitchen(order, printed=json.dumps({"DISH-FRIES": 2}), op="offlineSend2")
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Not Sent"])

	def test_a_bad_printed_value_records_nothing(self):
		order = self.order_with("one")
		self.assertEqual(floor.send_to_kitchen(order, printed=json.dumps(["DISH-FRIES"]), op="offlineSend3"), [])
		self.assertEqual([d.kitchen_status for d in self.lines(order)], ["Not Sent"])

	def test_the_floor_reads_the_bill_rules(self):
		rules = floor.get_floor()["bill_rules"]
		self.assertIn("taxes", rules)
		self.assertIn("disable_rounded_total", rules)
		self.assertIn("smallest_fraction", rules)
