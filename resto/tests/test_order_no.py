"""The order's number of the business day, the same on every kitchen ticket of the order (D-221).

bench --site test.resto run-tests --module resto.tests.test_order_no
"""

import datetime
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from resto.resto import floor, kitchen
from resto.tests import fixtures as fx


def tickets_of(order):
	return frappe.get_all(
		"Resto Kitchen Ticket",
		filters={"resto_order": order},
		fields=["name", "ticket_type", "order_no", "send_round"],
		order_by="creation asc",
	)


class TestOrderNo(IntegrationTestCase):
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
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", self._shift)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def order(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Numbered Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		return order

	def test_two_sends_and_a_void_carry_one_number(self):
		order = self.order()
		no = frappe.db.get_value("Resto Order", order, "order_no")
		self.assertTrue(no)
		floor.send_to_kitchen(order)
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "second"}]))
		floor.send_to_kitchen(order)
		line = next(d for d in frappe.get_doc("Resto Order", order).items if d.notes == "second")
		floor.void_line(order, line.name, "Changed mind")

		tickets = tickets_of(order)
		self.assertEqual([t.ticket_type for t in tickets], ["Order", "Order", "Void"])
		self.assertEqual({t.order_no for t in tickets}, {no})
		self.assertEqual([t.send_round for t in tickets], [1, 2, None])
		self.assertEqual(frappe.db.get_value("Resto Order", order, "send_rounds"), 2)

		# The kitchen screen gets them too.
		shown = {t["name"]: t for t in kitchen.get_tickets()}
		self.assertEqual(shown[tickets[1].name]["order_no"], no)
		self.assertEqual(shown[tickets[1].name]["send_round"], 2)

	def test_the_next_order_gets_the_next_number(self):
		first = frappe.db.get_value("Resto Order", self.order(), "order_no")
		second = frappe.db.get_value("Resto Order", self.order(), "order_no")
		self.assertEqual(second, first + 1)

	def test_the_number_starts_again_with_the_business_day(self):
		later = add_to_date(now_datetime(), days=400)
		key = frappe.cache.make_key(f"resto_order_no:{kitchen.business_day_start(later).date()}")
		frappe.cache.delete(key)
		try:
			self.assertEqual(kitchen.daily_order_no(later), 1)
			self.assertEqual(kitchen.daily_order_no(later), 2)
		finally:
			frappe.cache.delete(key)

	def test_a_moment_before_the_day_starts_belongs_to_the_day_before(self):
		with patch("resto.resto.demand.day_start_hour", return_value=6):
			night = datetime.datetime(2030, 3, 10, 3, 30)
			morning = datetime.datetime(2030, 3, 10, 6, 0)
			self.assertEqual(kitchen.business_day_start(night), datetime.datetime(2030, 3, 9, 6, 0))
			self.assertEqual(kitchen.business_day_start(morning), morning)


class TestDayTickets(IntegrationTestCase):
	"""The kitchen screen's Today's Tickets: found by the order's number or the table, brought back."""

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
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", self._shift)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def test_found_by_number_filtered_by_status_and_brought_back(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Day Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		no = frappe.db.get_value("Resto Order", order, "order_no")
		ticket = frappe.get_all("Resto Kitchen Ticket", filters={"resto_order": order}, pluck="name")[0]
		kitchen.set_ticket_status(ticket, "Served")

		found = kitchen.kds_day_tickets(search=str(no))["tickets"]
		self.assertIn(ticket, [t.name for t in found])
		self.assertEqual(next(t for t in found if t.name == ticket).dishes, ["1 × " + frappe.db.get_value("Item", "DISH-FRIES", "item_name")])
		self.assertNotIn(ticket, [t.name for t in kitchen.kds_day_tickets(search=str(no), status="New")["tickets"]])

		kitchen.kds_set_status(ticket, "New")
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", ticket, "status"), "New")
		self.assertIn(ticket, [t["name"] for t in kitchen.get_tickets()])

	def test_only_the_business_day(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Day Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		ticket = frappe.get_all("Resto Kitchen Ticket", filters={"resto_order": order}, pluck="name")[0]
		frappe.db.set_value("Resto Kitchen Ticket", ticket, "sent_at", add_to_date(now_datetime(), days=-2))
		no = frappe.db.get_value("Resto Order", order, "order_no")
		self.assertNotIn(ticket, [t.name for t in kitchen.kds_day_tickets(search=str(no))["tickets"]])
