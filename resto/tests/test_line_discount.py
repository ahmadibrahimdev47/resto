"""A discount on one order line (D-271): within the cashier's limit, above it with a manager's PIN,
100% for a dish given free (its stock issued all the same), with the order discount on top, and in
the control report."""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from resto.resto import approval, floor
from resto.resto.report.resto_voids_and_discounts.resto_voids_and_discounts import execute as voids_report
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import CASHIER, MANAGER_USER, _manager

PIN = "4321"


class TestLineDiscount(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()

	def setUp(self):
		frappe.flags.pop("resto_approvals", None)
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin(PIN)
		frappe.set_user("Administrator")
		self._limit = frappe.db.get_single_value("Resto Settings", "cashier_discount_limit")
		frappe.db.set_single_value("Resto Settings", {"require_pos_shift": 0, "cashier_discount_limit": 10})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		frappe.db.set_single_value("Resto Settings", "cashier_discount_limit", self._limit)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def order(self, *rows):
		order = floor.create_order(order_type="Takeaway", customer_name="Line Discount Guest")
		floor.add_items(order, json.dumps([r if isinstance(r, dict) else {"item_code": r[0], "qty": r[1]} for r in rows]))
		return order

	def line(self, order, item_code):
		return next(d for d in frappe.get_doc("Resto Order", order).items if d.item_code == item_code)

	def pay(self, order):
		bill = floor.get_bill(order)
		result = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))
		return frappe.get_doc("Sales Invoice", result["sales_invoice"])

	def test_within_the_limit_and_above_it_with_a_pin(self):
		order = self.order(("DISH-FRIES", 10))  # 400
		line = self.line(order, "DISH-FRIES").name
		frappe.set_user(CASHIER)
		floor.set_line_discount(order, line, 10)  # the reason is optional (D-274)
		row = self.line(order, "DISH-FRIES")
		self.assertEqual((flt(row.amount), flt(row.line_discount_amount), row.line_discount_by), (360, 40, CASHIER))
		self.assertFalse(row.line_discount_reason)
		self.assertEqual(flt(frappe.db.get_value("Resto Order", order, "net_total")), 360)
		# Above the cashier's 10%: a manager's PIN, for the value of the discount.
		self.assertRaises(approval.ApprovalRequired, floor.set_line_discount, order, line, 15, 0, "regular")
		self.assertRaises(approval.ApprovalRequired, floor.set_line_discount, order, line, 15, 0, "regular", MANAGER_USER, "9999")
		floor.set_line_discount(order, line, 0, 60, "regular", MANAGER_USER, PIN)  # an amount: 15%
		row = self.line(order, "DISH-FRIES")
		self.assertEqual((flt(row.line_discount_percentage), flt(row.amount), row.line_discount_by), (15, 340, MANAGER_USER))
		frappe.set_user("Administrator")
		logs = frappe.get_all("Resto Approval Log", filters={"reference": ("like", f"{order}%"), "result": "Approved"}, pluck="amount")
		self.assertEqual([flt(a, 2) for a in logs], [60])  # the 10% within the limit needed none
		# Smaller again: no new approval; 0 takes it off.
		frappe.set_user(CASHIER)
		floor.set_line_discount(order, line, 5, 0, "regular")
		floor.set_line_discount(order, line, 0)
		row = self.line(order, "DISH-FRIES")
		self.assertEqual((flt(row.line_discount_percentage), flt(row.amount), row.line_discount_by), (0, 400, None))

	def test_a_dish_given_free_issues_its_stock_and_posts_exactly(self):
		# The burger's doneness is required (fixtures); its extra cheese is a priced option, free with it.
		mods = [{"group": "Doneness", "option": "Medium"}, {"group": "Extras", "option": "Extra Cheese"}]
		order = self.order({"item_code": "DISH-BURGER", "qty": 1, "modifiers": mods}, ("DISH-FRIES", 2))  # 150 + 15 + 80
		floor.set_line_discount(order, self.line(order, "DISH-BURGER").name, 100, 0, "birthday")
		self.assertEqual(flt(frappe.db.get_value("Resto Order", order, "net_total")), 80)
		floor.send_to_kitchen(order)
		si = self.pay(order)
		burger = next(d for d in si.items if d.item_code == "DISH-BURGER")
		self.assertEqual((flt(burger.price_list_rate), flt(burger.rate), flt(burger.amount), flt(burger.discount_percentage)), (150, 0, 0, 100))
		self.assertEqual((burger.resto_discount_reason, burger.resto_discount_by), ("birthday", "Administrator"))
		cheese = next(d for d in si.items if d.item_code == "EXTRA-CHEESE")
		self.assertEqual((flt(cheese.price_list_rate), flt(cheese.rate), flt(cheese.discount_percentage)), (15, 0, 100))
		self.assertAlmostEqual(flt(si.total), 80, 2)
		# Its recipe is issued all the same, at cost.
		self.assertIn("RAW-BUN", {p.item_code for p in si.packed_items if p.parent_detail_docname == burger.name})
		bun = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": "Sales Invoice", "voucher_no": si.name, "item_code": "RAW-BUN", "is_cancelled": 0},
			fields=["actual_qty", "stock_value_difference"],
		)
		self.assertAlmostEqual(sum(flt(s.actual_qty) for s in bun), -1, 6)  # the bun only the burger uses
		self.assertLess(sum(flt(s.stock_value_difference) for s in bun), 0)
		gl = frappe.get_all("GL Entry", filters={"voucher_type": "Sales Invoice", "voucher_no": si.name, "is_cancelled": 0}, fields=["debit", "credit"])
		self.assertAlmostEqual(sum(flt(g.debit) for g in gl), sum(flt(g.credit) for g in gl), 2)
		# In the control report: at price, the discount and the cost.
		filters = {"company": fx.COMPANY, "from_date": nowdate(), "to_date": nowdate(), "type": "Item Discounts"}
		rows = [r for r in voids_report(filters)[1] if r.sales_invoice == si.name]
		self.assertEqual(sorted(r.item_code for r in rows), ["DISH-BURGER", "EXTRA-CHEESE"])
		r = next(r for r in rows if r.item_code == "DISH-BURGER")
		self.assertEqual((flt(r.value), flt(r.discount), flt(r.discount_percentage), r.reason), (150, 150, 100, "birthday"))
		self.assertGreater(flt(r.cost), 0)

	def test_the_order_discount_comes_on_top(self):
		order = self.order(("DISH-FRIES", 10))  # 400
		floor.set_line_discount(order, self.line(order, "DISH-FRIES").name, 10, 0, "regular")  # 360
		floor.apply_discount(order, 10)  # 36 off the 360; no reason needed (D-274)
		self.assertEqual(flt(frappe.db.get_value("Resto Order", order, "discount_amount")), 36)
		self.assertFalse(frappe.db.get_value("Resto Order", order, "discount_reason"))
		floor.send_to_kitchen(order)
		si = self.pay(order)
		fries = next(d for d in si.items if d.item_code == "DISH-FRIES")
		self.assertEqual((flt(fries.rate), flt(fries.amount)), (36, 360))
		self.assertEqual((flt(si.total), flt(si.discount_amount), flt(si.net_total)), (360, 36, 324))

	def test_more_dishes_take_the_discount_off(self):
		order = self.order(("DISH-FRIES", 2))
		line = self.line(order, "DISH-FRIES").name
		floor.set_line_discount(order, line, 50, 0, "regular")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))  # its own line
		lines = frappe.get_doc("Resto Order", order).items
		self.assertEqual(sorted((flt(d.qty), flt(d.line_discount_percentage)) for d in lines), [(1, 0), (2, 50)])
		floor.update_line(order, line, qty=1)  # fewer: kept
		self.assertEqual(flt(frappe.db.get_value("Resto Order Item", line, "line_discount_percentage")), 50)
		floor.update_line(order, line, step=1)  # more: taken off
		self.assertEqual(flt(frappe.db.get_value("Resto Order Item", line, "line_discount_percentage")), 0)

	def test_a_billed_line_keeps_its_price(self):
		order = self.order(("DISH-FRIES", 2))
		line = self.line(order, "DISH-FRIES").name
		floor.send_to_kitchen(order)
		self.pay(order)
		self.assertRaises(frappe.ValidationError, floor.set_line_discount, order, line, 10, 0, "late")
