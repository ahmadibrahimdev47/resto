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
SCREEN = json.dumps([STATION])


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
		frappe.cache.delete_value(kitchen._printer_key(STATION))

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

	def order(self, lines=1):
		order = floor.create_order(order_type="Takeaway", customer_name="Print Guest")
		floor.add_items(
			order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": f"line {i}"} for i in range(lines)])
		)
		return order

	def ticket(self, lines=1):
		order = self.order(lines)
		(name,) = floor.send_to_kitchen(order)
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", name, "station"), STATION)
		return name

	def printed(self, name):
		return frappe.db.get_value("Resto Kitchen Ticket", name, "printed")

	def test_automatic_prints_each_ticket_once_with_its_copies(self):
		self.station(kitchen.BROWSER_AUTO, copies=("Kitchen", "Pass"))
		name = self.ticket()
		claimed = kitchen.kds_claim_print(SCREEN)
		self.assertEqual(claimed["name"], name)
		pages = claimed["pages"]
		self.assertEqual(len(pages), 2)  # one page per copy, each with its label
		self.assertIn("*** Kitchen ***", pages[0])
		self.assertIn("*** Pass ***", pages[1])
		self.assertNotIn("*** Pass ***", pages[0])
		self.assertNotIn("REPRINT", pages[0])
		# Claimed, not printed yet: a second screen does not take it meanwhile.
		self.assertEqual(self.printed(name), 0)
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))
		kitchen.kds_printed(name)
		self.assertEqual(self.printed(name), 1)
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))

	def test_a_claim_never_confirmed_is_offered_again_as_a_reprint(self):
		self.station(kitchen.BROWSER_AUTO)
		name = self.ticket()
		self.assertEqual(kitchen.kds_claim_print(SCREEN)["name"], name)
		# The computer restarted before the ticket printed.
		frappe.db.set_value(
			"Resto Kitchen Ticket", name, "print_claimed_at", add_to_date(now_datetime(), seconds=-(kitchen.CLAIM_SECONDS + 5))
		)
		again = kitchen.kds_claim_print(SCREEN)
		self.assertEqual(again["name"], name)
		self.assertIn("REPRINT", again["pages"][0])

	def test_only_stations_of_the_screen_and_that_print_automatically(self):
		self.station(kitchen.BROWSER_AUTO)
		self.ticket()
		self.assertIsNone(kitchen.kds_claim_print(json.dumps(["Some Other Station"])))
		self.station(kitchen.BROWSER_BUTTON)
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))

	def test_old_tickets_do_not_print_by_themselves_and_are_flagged(self):
		self.station(kitchen.BROWSER_AUTO, minutes=30)
		name = self.ticket()
		frappe.db.set_value("Resto Kitchen Ticket", name, "sent_at", add_to_date(now_datetime(), hours=-2))
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))
		self.assertEqual(self.printed(name), 0)
		self.assertIn(name, [t.name for t in kitchen.print_alerts()["unprinted"]])

	def test_a_ticket_already_made_is_not_printed_late(self):
		self.station(kitchen.BROWSER_AUTO)
		name = self.ticket()
		kitchen.kds_set_status(name, "Ready")
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))

	def test_late_tickets_say_so(self):
		self.station(kitchen.BROWSER_AUTO)
		name = self.ticket()
		frappe.db.set_value("Resto Kitchen Ticket", name, "sent_at", add_to_date(now_datetime(), minutes=-10))
		self.assertIn("LATE", kitchen.kds_claim_print(SCREEN)["pages"][0])

	def test_void_prints_when_the_original_was_on_paper(self):
		self.station(kitchen.BROWSER_AUTO)
		order = self.order()
		(original,) = floor.send_to_kitchen(order)
		kitchen.kds_claim_print(SCREEN)
		kitchen.kds_printed(original)
		floor.void_line(order, frappe.get_doc("Resto Order", order).items[0].name, "Wrong dish")
		claimed = kitchen.kds_claim_print(SCREEN)
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", claimed["name"], "ticket_type"), "Void")
		self.assertIn("DO NOT PREPARE", claimed["pages"][0])
		self.assertIn(original, claimed["pages"][0])  # which ticket it cancels

	def test_void_of_a_ticket_never_printed_does_not_print(self):
		self.station(kitchen.BROWSER_AUTO)
		order = self.order()
		(original,) = floor.send_to_kitchen(order)
		floor.void_line(order, frappe.get_doc("Resto Order", order).items[0].name, "Wrong dish")
		# The original is cancelled (nothing left on it) and the void has nothing to cancel on paper.
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))
		void = frappe.get_value("Resto Kitchen Ticket", {"resto_order": order, "ticket_type": "Void"}, "name")
		self.assertEqual(self.printed(void), 1)
		self.assertEqual(self.printed(original), 0)

	def test_voided_lines_print_struck(self):
		self.station(kitchen.BROWSER_BUTTON)
		order = self.order(lines=2)
		(name,) = floor.send_to_kitchen(order)
		floor.void_line(order, frappe.get_doc("Resto Order", order).items[0].name, "Wrong dish")
		page = kitchen.kds_print(name)["pages"][0]
		self.assertEqual(page.count("VOIDED"), 1)

	def test_print_button_works_in_every_method_and_marks_reprints(self):
		self.station(kitchen.BROWSER_BUTTON)
		name = self.ticket()
		self.assertIsNone(kitchen.kds_claim_print(SCREEN))  # the button prints, not the screen
		self.assertNotIn("REPRINT", kitchen.kds_print(name)["pages"][0])
		kitchen.kds_printed(name)
		self.assertIn("REPRINT", kitchen.kds_print(name)["pages"][0])
		self.station(kitchen.NO_PRINTING)
		self.assertEqual(len(kitchen.kds_print(name)["pages"]), 1)

	def test_station_numbers_follow_each_other(self):
		self.station(kitchen.BROWSER_BUTTON)
		first = frappe.db.get_value("Resto Kitchen Ticket", self.ticket(), "daily_no")
		second = frappe.db.get_value("Resto Kitchen Ticket", self.ticket(), "daily_no")
		self.assertTrue(first >= 1)
		self.assertEqual(second, first + 1)

	def test_kitchen_screens_warn_when_no_screen_prints(self):
		self.station(kitchen.BROWSER_AUTO)
		self.assertIn(STATION, kitchen.print_alerts()["no_screen"])
		kitchen.kds_claim_print(SCREEN)  # a screen printing for the station checks in
		self.assertNotIn(STATION, kitchen.print_alerts()["no_screen"])

	def test_server_printer_needs_a_printer(self):
		station = frappe.get_doc("Resto Kitchen Station", STATION)
		station.print_method = kitchen.SERVER_PRINTER
		station.printer = None
		self.assertRaises(frappe.ValidationError, station.save)
