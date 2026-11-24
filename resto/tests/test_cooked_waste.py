"""Waste for dishes the kitchen made of an order cancelled after sending ("Already cooked?").

bench --site test.resto run-tests --module resto.tests.test_cooked_waste
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from resto.resto import floor, kitchen
from resto.resto.report.resto_kitchen_vs_invoices import resto_kitchen_vs_invoices as report
from resto.tests import fixtures as fx


class TestCookedWaste(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def tearDown(self):
		frappe.set_user("Administrator")

	def sent_order(self, lines=2):
		order = floor.create_order(order_type="Takeaway", customer_name="Waste Guest")
		floor.add_items(
			order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": f"dish {i}"} for i in range(lines)])
		)
		floor.send_to_kitchen(order)
		return order, [d.name for d in frappe.get_doc("Resto Order", order).items]

	def waste_of(self, line):
		return frappe.db.get_value("Resto Order Item", line, "void_waste")

	def test_cancel_posts_waste_only_for_the_cooked_dishes(self):
		order, (cooked, not_cooked) = self.sent_order()
		floor.cancel_order(order, "Guest left", cooked=json.dumps([cooked]))
		waste = self.waste_of(cooked)
		self.assertTrue(waste)
		self.assertEqual(frappe.db.get_value("Resto Waste", waste, "docstatus"), 1)
		self.assertFalse(self.waste_of(not_cooked))
		rows = {
			r.name: r
			for r in report.execute({"company": fx.COMPANY, "from_date": nowdate(), "to_date": nowdate()})[1]
			if r.resto_order == order
		}
		self.assertEqual(rows[cooked].result, report.CANCELLED)
		self.assertGreater(rows[cooked].waste_cost, 0)
		self.assertFalse(rows[not_cooked].void_waste)

	def test_only_sent_dishes_can_be_cooked(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Waste Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		line = frappe.get_doc("Resto Order", order).items[0].name
		self.assertRaises(frappe.ValidationError, floor.cancel_order, order, "Guest left", json.dumps([line]))

	def test_kitchen_posts_it_later_from_the_void_ticket_once(self):
		order, (line, _other) = self.sent_order()
		floor.cancel_order(order, "Guest left")
		# The kitchen screen offers it on the void ticket.
		void = frappe.get_value("Resto Kitchen Ticket", {"resto_order": order, "ticket_type": "Void"}, "name")
		(card,) = [t for t in kitchen.get_tickets() if t["name"] == void]
		self.assertTrue(all(d["waste_open"] for d in card["items"]))
		frappe.set_user("kitchen@resto.test")
		posted = floor.post_cooked_waste(order, json.dumps([line]))
		self.assertTrue(posted[line])
		self.assertEqual(self.waste_of(line), posted[line])
		self.assertRaises(frappe.ValidationError, floor.post_cooked_waste, order, json.dumps([line]))
		frappe.set_user("Administrator")
		(card,) = [t for t in kitchen.get_tickets() if t["name"] == void]
		self.assertEqual({d["order_item"]: d["waste_open"] for d in card["items"]}[line], 0)

	def test_after_hours_only_a_manager(self):
		order, (line, _other) = self.sent_order()
		floor.cancel_order(order, "Guest left")
		frappe.db.sql(
			"update `tabResto Kitchen Ticket` set sent_at = %s where resto_order = %s and ticket_type = 'Void'",
			(add_to_date(now_datetime(), hours=-(floor.COOKED_WASTE_HOURS + 1)), order),
		)
		frappe.set_user("cashier@resto.test")
		self.assertRaises(frappe.ValidationError, floor.post_cooked_waste, order, json.dumps([line]))
		frappe.set_user("Administrator")
		self.assertTrue(floor.post_cooked_waste(order, json.dumps([line]))[line])

	def test_a_waiter_cannot_post_it(self):
		order, (line, _other) = self.sent_order()
		floor.cancel_order(order, "Guest left")
		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.PermissionError, floor.post_cooked_waste, order, json.dumps([line]))

	def test_a_line_still_on_the_order_cannot_be_posted(self):
		order, (line, _other) = self.sent_order()
		self.assertRaises(frappe.ValidationError, floor.post_cooked_waste, order, json.dumps([line]))
