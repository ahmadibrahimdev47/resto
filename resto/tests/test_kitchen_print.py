"""Kitchen tickets printed from the browser (Resto Kitchen Station "Printing Method").

bench --site test.resto run-tests --module resto.tests.test_kitchen_print
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from resto.resto import floor, kitchen
from resto.tests import fixtures as fx

STATION = "Grill"


class TestKitchenPrint(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		station = frappe.get_doc("Resto Kitchen Station", STATION)
		self.saved = {
			"print_method": station.print_method,
			"printer": station.printer,
			"auto_print": station.auto_print,
			"auto_print_minutes": station.auto_print_minutes,
			"copies": [r.label for r in station.copies],
		}
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")
		# Older test tickets must not be claimed here.
		frappe.db.sql("update `tabResto Kitchen Ticket` set printed = 1 where printed = 0")

	def tearDown(self):
		station = frappe.get_doc("Resto Kitchen Station", STATION)
		station.update({k: v for k, v in self.saved.items() if k != "copies"})
		station.set("copies", [{"label": label} for label in self.saved["copies"]])
		station.save(ignore_permissions=True)

	def station(self, method, copies=(), minutes=30):
		station = frappe.get_doc("Resto Kitchen Station", STATION)
		station.print_method = method
		station.auto_print_minutes = minutes
		station.set("copies", [{"label": label} for label in copies])
		station.save(ignore_permissions=True)

	def ticket(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Print Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		(name,) = floor.send_to_kitchen(order)
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", name, "station"), STATION)
		return name

	def test_automatic_prints_each_ticket_once_with_its_copies(self):
		self.station(kitchen.BROWSER_AUTO, copies=("Kitchen", "Pass"))
		name = self.ticket()
		self.assertEqual(kitchen.kds_data(STATION)["to_print"], 1)
		claimed = kitchen.kds_claim_prints(STATION)
		self.assertEqual([t["name"] for t in claimed], [name])
		pages = claimed[0]["pages"]
		self.assertEqual(len(pages), 2)  # one page per copy, each with its label
		self.assertIn("*** Kitchen ***", pages[0])
		self.assertIn("*** Pass ***", pages[1])
		self.assertNotIn("*** Pass ***", pages[0])
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", name, "printed"), 1)
		# A second screen (or a reload) never prints it again.
		self.assertEqual(kitchen.kds_claim_prints(STATION), [])
		self.assertEqual(kitchen.kds_data(STATION)["to_print"], 0)

	def test_old_tickets_are_not_printed_when_a_screen_opens(self):
		self.station(kitchen.BROWSER_AUTO, minutes=30)
		name = self.ticket()
		frappe.db.set_value("Resto Kitchen Ticket", name, "sent_at", add_to_date(now_datetime(), hours=-12))
		self.assertEqual(kitchen.kds_claim_prints(STATION), [])
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", name, "printed"), 0)

	def test_void_tickets_print_too(self):
		self.station(kitchen.BROWSER_AUTO)
		order = floor.create_order(order_type="Takeaway", customer_name="Print Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		kitchen.kds_claim_prints(STATION)
		line = frappe.get_doc("Resto Order", order).items[0].name
		floor.void_line(order, line, "Wrong dish")
		claimed = kitchen.kds_claim_prints(STATION)
		self.assertEqual(len(claimed), 1)
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", claimed[0]["name"], "ticket_type"), "Void")

	def test_button_and_no_printing(self):
		self.station(kitchen.BROWSER_BUTTON)
		name = self.ticket()
		self.assertEqual(kitchen.kds_data(STATION)["to_print"], 0)  # the button prints, not the screen
		self.assertEqual(len(kitchen.kds_print(name)["pages"]), 1)
		self.station(kitchen.NO_PRINTING)
		self.assertRaises(frappe.ValidationError, kitchen.kds_print, name)

	def test_server_printer_needs_a_printer(self):
		station = frappe.get_doc("Resto Kitchen Station", STATION)
		station.print_method = kitchen.SERVER_PRINTER
		station.printer = None
		self.assertRaises(frappe.ValidationError, station.save)
