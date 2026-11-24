"""Print copies. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_printing
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import printing
from resto.tests import fixtures as fx

PRINTER = "Resto Test Printer"


class TestPrinting(IntegrationTestCase):
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
		self.settings = frappe.get_doc("Resto Settings")
		self.saved = {
			"cashier_print_method": self.settings.cashier_print_method,
			"cashier_printer": self.settings.cashier_printer,
			"print_bill_on_printer": self.settings.print_bill_on_printer,
			"print_receipt_on_printer": self.settings.print_receipt_on_printer,
			"bill_copies": [r.label for r in self.settings.bill_copies],
			"receipt_copies": [r.label for r in self.settings.receipt_copies],
		}

	def tearDown(self):
		settings = frappe.get_doc("Resto Settings")
		for key in ("cashier_print_method", "cashier_printer", "print_bill_on_printer", "print_receipt_on_printer"):
			settings.set(key, self.saved[key])
		for table in ("bill_copies", "receipt_copies"):
			settings.set(table, [{"label": label} for label in self.saved[table]])
		settings.save()

	def configure(self, bill=(), receipt=()):
		settings = frappe.get_doc("Resto Settings")
		settings.cashier_print_method = printing.SERVER_PRINTER
		settings.cashier_printer = PRINTER
		settings.print_bill_on_printer = 1
		settings.print_receipt_on_printer = 1
		settings.set("bill_copies", [{"label": label} for label in bill])
		settings.set("receipt_copies", [{"label": label} for label in receipt])
		settings.save()

	def test_copy_labels(self):
		self.assertEqual(printing.copy_labels([]), [""])
		rows = [frappe._dict(label=" Customer Copy "), frappe._dict(label=None)]
		self.assertEqual(printing.copy_labels(rows), ["Customer Copy", ""])

	def test_one_job_per_copy_with_its_label(self):
		seen = []

		def fake(doctype, name, printer, print_format, doc=None, no_letterhead=0, **kw):
			seen.append((printer, print_format, doc.resto_copy_label))

		with patch("frappe.utils.print_format.print_by_server", fake):
			printing.print_copies(
				"Resto Settings", "Resto Settings", PRINTER, "Resto Pre-Bill", ["Customer Copy", "Restaurant Copy"]
			)
		self.assertEqual(
			seen,
			[(PRINTER, "Resto Pre-Bill", "Customer Copy"), (PRINTER, "Resto Pre-Bill", "Restaurant Copy")],
		)

	def test_queue_only_when_printer_is_on(self):
		settings = frappe.get_doc("Resto Settings")
		settings.cashier_print_method = printing.BROWSER_BUTTON
		settings.cashier_printer = None
		settings.print_bill_on_printer = 0
		settings.print_receipt_on_printer = 0
		settings.save()
		self.assertFalse(printing.queue_bill("anything"))
		self.assertFalse(printing.queue_receipt("anything"))

		self.configure(bill=["Customer Copy", "Restaurant Copy"], receipt=["Customer Copy"])
		with patch("frappe.enqueue") as enqueue:
			self.assertTrue(printing.queue_bill("ORDER-1"))
			self.assertTrue(printing.queue_receipt("SINV-1", None))
		self.assertEqual(enqueue.call_count, 2)
		self.assertEqual(printing.copy_labels(printing.get_settings().bill_copies), ["Customer Copy", "Restaurant Copy"])

	def test_print_bill_uses_bill_copies(self):
		self.configure(bill=["Customer Copy", "Restaurant Copy"])
		with patch("resto.resto.printing.print_copies") as print_copies:
			printing.print_bill("ORDER-1")
		args = print_copies.call_args.args
		self.assertEqual(args[:4], ("Resto Order", "ORDER-1", PRINTER, "Resto Pre-Bill"))
		self.assertEqual([c["label"] for c in args[4]], ["Customer Copy", "Restaurant Copy"])
