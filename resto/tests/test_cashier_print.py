"""Bills and receipts printed from the cashier's browser (Resto Settings "Cashier Printing Method").

bench --site test.resto run-tests --module resto.tests.test_cashier_print
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import floor, printing
from resto.tests import fixtures as fx

PRINTER = "Resto Test Printer"


class TestCashierPrint(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		if not frappe.db.exists("Network Printer Settings", PRINTER):
			frappe.get_doc(
				{
					"doctype": "Network Printer Settings",
					"name": PRINTER,
					"server_ip": "127.0.0.1",
					"port": 631,
					"printer_name": "resto-test",
				}
			).insert(ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		settings = frappe.get_doc("Resto Settings")
		self.saved = {
			key: settings.get(key)
			for key in ("cashier_print_method", "cashier_printer", "print_bill_on_printer", "print_receipt_on_printer")
		}
		self.saved_copies = {t: [r.label for r in settings.get(t)] for t in ("bill_copies", "receipt_copies")}

	def tearDown(self):
		settings = frappe.get_doc("Resto Settings")
		settings.update(self.saved)
		for table, labels in self.saved_copies.items():
			settings.set(table, [{"label": label} for label in labels])
		settings.save()

	def configure(self, method, printer=None, bill=(), receipt=()):
		settings = frappe.get_doc("Resto Settings")
		settings.cashier_print_method = method
		settings.cashier_printer = printer
		settings.print_bill_on_printer = 1 if printer else 0
		settings.print_receipt_on_printer = 1 if printer else 0
		settings.set("bill_copies", [{"label": label} for label in bill])
		settings.set("receipt_copies", [{"label": label} for label in receipt])
		settings.save()

	def order(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Print Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		return order

	def pay(self, order, request_id):
		bill = floor.get_bill(order)
		return floor.settle(
			order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]), None, bill["payable"], request_id
		)

	def test_modes(self):
		self.configure(printing.SERVER_PRINTER, PRINTER)
		self.assertEqual((printing.bill_mode(), printing.receipt_mode()), ("server", "server"))
		self.configure(printing.BROWSER_AUTO)
		self.assertEqual((printing.bill_mode(), printing.receipt_mode()), ("browser", "auto"))
		self.configure(printing.BROWSER_BUTTON)
		self.assertEqual((printing.bill_mode(), printing.receipt_mode()), ("browser", "button"))
		self.configure(printing.NO_PRINTING)
		self.assertEqual((printing.bill_mode(), printing.receipt_mode()), ("none", "none"))
		# A printer left in the settings does not print in another method.
		self.configure(printing.BROWSER_BUTTON, PRINTER)
		self.assertFalse(printing.queue_bill("anything"))
		self.assertFalse(printing.queue_receipt("anything"))

	def test_server_printer_needs_a_printer(self):
		settings = frappe.get_doc("Resto Settings")
		settings.cashier_print_method = printing.SERVER_PRINTER
		settings.cashier_printer = None
		self.assertRaises(frappe.ValidationError, settings.save)

	def test_bill_prints_all_its_copies_in_the_browser(self):
		self.configure(printing.BROWSER_BUTTON, bill=("Customer Copy", "Restaurant Copy"))
		bill = floor.request_bill(self.order())
		self.assertFalse(bill["on_printer"])
		self.assertEqual(len(bill["pages"]), 2)
		self.assertIn("Customer Copy", bill["pages"][0])
		self.assertIn("Restaurant Copy", bill["pages"][1])
		self.configure(printing.NO_PRINTING)
		self.assertEqual(floor.request_bill(self.order())["pages"], [])

	def test_bill_on_the_server_printer_has_no_pages(self):
		self.configure(printing.SERVER_PRINTER, PRINTER)
		with patch("frappe.enqueue"):
			bill = floor.request_bill(self.order())
		self.assertTrue(bill["on_printer"])
		self.assertEqual(bill["pages"], [])

	def test_receipt_prints_once_by_itself(self):
		self.configure(printing.BROWSER_AUTO, receipt=("Customer Copy", "Restaurant Copy"))
		order = self.order()
		first = self.pay(order, "cashier-print-1")
		self.assertEqual(first["receipt_mode"], "auto")
		self.assertEqual(first["receipt_printed"], 0)
		self.assertEqual(len(printing.receipt_pages(first["sales_invoice"])), 2)
		printing.receipt_printed(first["sales_invoice"])
		# The answer was lost and the cashier clicked again: same invoice, already printed.
		again = self.pay(order, "cashier-print-1")
		self.assertEqual(again["sales_invoice"], first["sales_invoice"])
		self.assertEqual(again["receipt_printed"], 1)
		# Print Again still prints it.
		self.assertEqual(len(printing.receipt_pages(first["sales_invoice"])), 2)

	def test_receipt_pages_only_for_restaurant_invoices(self):
		self.assertRaises(frappe.ValidationError, printing.receipt_pages, "no-such-invoice")
