"""Report "Resto Kitchen vs Invoices": what the kitchen got against what was billed.

bench --site test.resto run-tests --module resto.tests.test_kitchen_vs_invoices
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from resto.resto import floor
from resto.resto.report.resto_kitchen_vs_invoices import resto_kitchen_vs_invoices as report
from resto.tests import fixtures as fx


class TestKitchenVsInvoices(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")

	def run_report(self, **filters):
		_columns, data, _m, _c, summary = report.execute({"company": fx.COMPANY, "from_date": nowdate(), "to_date": nowdate(), **filters})
		return data, {s["label"]: s["value"] for s in summary}

	def rows(self, order, **filters):
		data, _summary = self.run_report(**filters)
		return [d for d in data if d.resto_order == order]

	def sent(self, order_type="Takeaway", items=None, table=None):
		if order_type == "Dine In":
			order = floor.create_order(order_type="Dine In", table=table, guests=2)
		else:
			order = floor.create_order(order_type="Takeaway", customer_name="Report Guest")
		floor.add_items(order, json.dumps(items or [{"item_code": "DISH-FRIES", "qty": 2}]))
		floor.send_to_kitchen(order)
		return order

	def pay(self, order):
		bill = floor.get_bill(order)
		return floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))

	def test_billed_lines_only_in_all_lines(self):
		order = self.sent()
		self.pay(order)
		(row,) = self.rows(order, show="All Lines")
		self.assertEqual(row.result, report.BILLED)
		self.assertEqual((row.qty, row.billed_qty, row.unbilled_qty), (2, 2, 0))
		self.assertTrue(row.sales_invoice)
		self.assertEqual(self.rows(order), [])  # nothing to look at

	def test_open_order_is_sent_not_billed(self):
		order = self.sent()
		(row,) = self.rows(order)
		self.assertEqual(row.result, report.OPEN)
		self.assertEqual(row.unbilled_qty, 2)
		self.assertAlmostEqual(row.unbilled_amount, row.amount)

	def test_order_cancelled_after_sending(self):
		order = self.sent()
		floor.cancel_order(order, "Guest left")
		(row,) = self.rows(order)
		self.assertEqual(row.result, report.CANCELLED)
		self.assertEqual(row.unbilled_qty, 2)
		self.assertEqual(row.voided_by, "Administrator")
		self.assertTrue(row.voided_at)
		_data, summary = self.run_report()
		self.assertGreaterEqual(summary[frappe._("Sent, Order Cancelled")], row.amount)

	def test_void_after_cooking_shows_who_why_and_waste(self):
		order = self.sent("Dine In", [{"item_code": "DISH-FRIES", "qty": 1}], table="T5")
		line = frappe.get_doc("Resto Order", order).items[0].name
		floor.void_line(order, line, "Dropped", cooked=1)
		(row,) = self.rows(order)
		self.assertEqual(row.result, report.VOIDED)
		self.assertEqual(row.void_reason, "Dropped")
		self.assertEqual(row.voided_by, "Administrator")
		self.assertTrue(row.voided_at)
		self.assertTrue(row.void_waste)
		self.assertGreater(row.waste_cost, 0)
		self.assertEqual(row.unbilled_qty, 0)  # a void is not money missing

	def test_station_and_cashier_filters(self):
		order = self.sent()
		self.pay(order)
		self.assertEqual(len(self.rows(order, show="All Lines", station="Grill")), 1)
		self.assertEqual(self.rows(order, show="All Lines", station="No Such Station"), [])
		self.assertEqual(len(self.rows(order, show="All Lines", cashier="Administrator")), 1)
		self.assertEqual(self.rows(order, show="All Lines", cashier="Guest"), [])
