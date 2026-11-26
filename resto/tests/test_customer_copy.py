"""The customer's copy of the bill and receipt leaves out the items given free (D-272) when Resto
Settings says so; the restaurant copy, the invoice and the reports keep them."""

import json

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from resto.resto import floor, printing
from resto.tests import fixtures as fx

COPIES = [
	{"label": "Customer Copy", "show_sides": 0, "customer_copy": 1},
	{"label": "Restaurant Copy", "show_sides": 0, "customer_copy": 0},
]
FIELDS = ("cashier_print_method", "receipt_print_format", "hide_free_lines_for_customer", "free_lines_note")


class TestCustomerCopy(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		settings = frappe.get_doc("Resto Settings")
		saved = {f: settings.get(f) for f in FIELDS}
		tables = {t: [r.as_dict() for r in settings.get(t)] for t in ("bill_copies", "receipt_copies")}

		def restore():
			s = frappe.get_doc("Resto Settings")
			s.update(saved)
			for t, rows in tables.items():
				s.set(t, [{k: r.get(k) for k in ("label", "show_sides", "customer_copy")} for r in rows])
			s.save()

		self.addCleanup(restore)
		settings.update(
			{
				"require_pos_shift": 0,
				"cashier_print_method": printing.BROWSER_BUTTON,
				"receipt_print_format": printing.RECEIPT_FORMAT,
				"hide_free_lines_for_customer": 1,
				"free_lines_note": 1,
			}
		)
		settings.set("bill_copies", COPIES)
		settings.set("receipt_copies", COPIES)
		settings.save()

	def free_burger_order(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Copy Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-BURGER", "qty": 1}, {"item_code": "DISH-FRIES", "qty": 2}]))
		burger = next(d for d in frappe.get_doc("Resto Order", order).items if d.item_code == "DISH-BURGER")
		floor.set_line_discount(order, burger.name, 100, 0, "birthday")
		floor.send_to_kitchen(order)
		return order

	def pay(self, order):
		bill = floor.get_bill(order)
		result = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))
		return frappe.get_doc("Sales Invoice", result["sales_invoice"])

	def test_the_customer_copy_leaves_out_what_was_given_free(self):
		order = self.free_burger_order()
		note = _("Includes items given free")
		customer, restaurant = floor.request_bill(order)["pages"]
		self.assertNotIn("Dish Burger", customer)
		self.assertIn("Dish Fries", customer)
		self.assertIn(note, customer)
		self.assertIn("Dish Burger", restaurant)
		self.assertNotIn(note, restaurant)
		si = self.pay(order)
		customer, restaurant = printing.receipt_pages(si.name)
		self.assertNotIn("Dish Burger", customer)
		self.assertIn(note, customer)
		self.assertIn("Dish Burger", restaurant)
		# The invoice keeps the dish given free, at 0.
		self.assertEqual([flt(d.amount) for d in si.items if d.item_code == "DISH-BURGER"], [0])
		# Without the note; and with the switch off every copy shows it.
		frappe.db.set_single_value("Resto Settings", "free_lines_note", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		customer = printing.receipt_pages(si.name)[0]
		self.assertNotIn("Dish Burger", customer)
		self.assertNotIn(note, customer)
		frappe.db.set_single_value("Resto Settings", "hide_free_lines_for_customer", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		for page in printing.receipt_pages(si.name):
			self.assertIn("Dish Burger", page)

	def test_one_copy_without_rows_is_the_customers(self):
		self.assertEqual(printing.copy_specs([]), [{"label": "", "show_sides": 0, "customer_copy": 1}])
		order = self.free_burger_order()
		settings = frappe.get_doc("Resto Settings")
		settings.set("receipt_copies", [])
		settings.save()
		(page,) = printing.receipt_pages(self.pay(order).name)
		self.assertNotIn("Dish Burger", page)
		self.assertIn("Dish Fries", page)
