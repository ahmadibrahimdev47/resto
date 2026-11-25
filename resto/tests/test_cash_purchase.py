"""Cash purchases from the drawer (D-225, resto/resto/shift.py cash_purchase).

Food bought for cash by the cashier during the shift: a paid Purchase Invoice that receives it into
the kitchen at what was paid and pays it from the drawer. Stock, the ledger and the shift's expected
cash must stay exact. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_cash_purchase
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from resto.resto import approval, shift
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import CASHIER, MANAGER_USER, _manager

BOUGHT = [{"item_code": "RAW-BEEF", "qty": 0.5, "amount": 150}, {"item_code": "RAW-BUN", "qty": 3, "amount": 10}]


def sle_value(pi):
	return flt(
		frappe.db.sql(
			"""select sum(stock_value_difference) from `tabStock Ledger Entry`
			where voucher_type = 'Purchase Invoice' and voucher_no = %s and is_cancelled = 0""",
			pi,
		)[0][0]
	)


def gl(pi):
	out = {}
	for r in frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Purchase Invoice", "voucher_no": pi, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		out[r.account] = out.get(r.account, 0) + flt(r.debit) - flt(r.credit)
	return out


def stock_account():
	from erpnext.stock import get_warehouse_account_map

	return get_warehouse_account_map(fx.COMPANY)[fx.KITCHEN].account


def cash_account():
	return frappe.db.get_value("Mode of Payment Account", {"parent": "Cash", "company": fx.COMPANY}, "default_account")


class TestCashPurchase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in ("require_pos_shift", "blind_shift_count", "drawer_purchase_cap")}
		frappe.db.set_single_value("Resto Settings", {"require_pos_shift": 0, "blind_shift_count": 0, "drawer_purchase_cap": 0})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		if frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile"}) and not frappe.db.exists(
			"POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}
		):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.set_user(CASHIER)
		self.shift = shift.open_shift(json.dumps({"Cash": 500}))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", self._kept)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.flags.pop("resto_approvals", None)

	def expected_cash(self):
		return next(r for r in shift.closing_data()["rows"] if r["mode_of_payment"] == "Cash")["expected_amount"]

	def test_stock_lands_at_the_paid_rate_and_the_drawer_pays(self):
		before = {c: fx.bin_qty(c) for c in ("RAW-BEEF", "RAW-BUN")}
		expected = self.expected_cash()
		name = shift.cash_purchase(json.dumps(BOUGHT), "Cash", request_id="buy-1", remarks="Market")
		pi = frappe.get_doc("Purchase Invoice", name)
		self.assertEqual((pi.docstatus, pi.is_paid, pi.update_stock), (1, 1, 1))
		self.assertAlmostEqual(flt(pi.grand_total), 160, 2)
		self.assertAlmostEqual(flt(pi.outstanding_amount), 0, 2)
		self.assertFalse(pi.taxes)
		# Into the kitchen, at what was paid (3 buns for 10: the cents above it are the discount).
		self.assertAlmostEqual(fx.bin_qty("RAW-BEEF") - before["RAW-BEEF"], 0.5, 6)
		self.assertAlmostEqual(fx.bin_qty("RAW-BUN") - before["RAW-BUN"], 3, 6)
		self.assertAlmostEqual(sle_value(name), 160, 2)
		# The ledger: stock up by the stock ledger value, the cash account down by what was paid.
		g = gl(name)
		self.assertAlmostEqual(sum(g.values()), 0, 2)
		self.assertAlmostEqual(g.get(stock_account(), 0), sle_value(name), 2)
		self.assertAlmostEqual(g.get(cash_account(), 0), -160, 2)
		# The shift: the drawer should now hold 160 less, listed apart from the expenses.
		self.assertAlmostEqual(self.expected_cash(), expected - 160, 2)
		data = shift.closing_data()
		self.assertAlmostEqual(data["summary"]["purchases_total"], 160, 2)
		self.assertEqual(data["summary"]["expenses_total"], 0)
		self.assertEqual([p["purchase_invoice"] for p in data["purchases"]], [name])
		# No supplier named: the cash supplier, kept in Resto Settings.
		self.assertEqual(pi.supplier, frappe.db.get_single_value("Resto Settings", "cash_supplier"))

	def test_the_same_request_again_makes_nothing(self):
		first = shift.cash_purchase(json.dumps(BOUGHT), "Cash", request_id="buy-twice")
		count = frappe.db.count("Purchase Invoice", {"docstatus": 1})
		self.assertEqual(shift.cash_purchase(json.dumps(BOUGHT), "Cash", request_id="buy-twice"), first)
		self.assertEqual(frappe.db.count("Purchase Invoice", {"docstatus": 1}), count)
		self.assertEqual(len(frappe.get_doc("Resto Shift", self.shift).purchases), 1)

	def test_refused_more_than_the_drawer_or_not_stock(self):
		big = [{"item_code": "RAW-BEEF", "qty": 1, "amount": 100000}]
		self.assertRaises(frappe.ValidationError, shift.cash_purchase, json.dumps(big), "Cash")
		dish = [{"item_code": "DISH-FRIES", "qty": 1, "amount": 10}]
		self.assertRaises(frappe.ValidationError, shift.cash_purchase, json.dumps(dish), "Cash")
		self.assertRaises(frappe.ValidationError, shift.cash_purchase, json.dumps([{"item_code": "RAW-BUN", "qty": 0, "amount": 5}]), "Cash")
		self.assertRaises(frappe.ValidationError, shift.cash_purchase, json.dumps(BOUGHT), "Card")
		# A drawer expense still cannot go to the stock or cost of goods sold accounts.
		stock_like = frappe.db.get_value(
			"Account", {"company": fx.COMPANY, "is_group": 0, "account_type": ("in", ("Cost of Goods Sold", "Stock Adjustment"))}, "name"
		)
		if stock_like:
			self.assertRaises(frappe.ValidationError, shift.add_expense, "Cash", stock_like, 5, "Tomatoes")

	def test_above_the_limit_a_manager_approves(self):
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "drawer_purchase_cap", 100)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		self.assertEqual(shift.purchase_data()["approval_above"], 100)
		self.assertRaises(approval.ApprovalRequired, shift.cash_purchase, json.dumps(BOUGHT), "Cash")
		name = shift.cash_purchase(json.dumps(BOUGHT), "Cash", approver=MANAGER_USER, pin="4321")
		row = next(p for p in frappe.get_doc("Resto Shift", self.shift).purchases if p.purchase_invoice == name)
		self.assertEqual(row.approved_by, MANAGER_USER)
		# Under the limit: no approval.
		shift.cash_purchase(json.dumps([{"item_code": "RAW-BUN", "qty": 2, "amount": 10}]), "Cash")

	def test_a_price_far_from_the_usual_needs_a_manager(self):
		# 500 typed for 500 g of a Kg item: 0.30 per Kg against the usual 300.
		grams = [{"item_code": "RAW-BEEF", "qty": 500, "amount": 150}]
		self.assertRaises(approval.ApprovalRequired, shift.cash_purchase, json.dumps(grams), "Cash")
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user(CASHIER)
		self.assertTrue(shift.cash_purchase(json.dumps(grams), "Cash", approver=MANAGER_USER, pin="4321"))

	def test_the_same_item_twice_is_one_line(self):
		twice = [{"item_code": "RAW-BUN", "qty": 2, "amount": 10}, {"item_code": "RAW-BUN", "qty": 1, "amount": 5}]
		pi = frappe.get_doc("Purchase Invoice", shift.cash_purchase(json.dumps(twice), "Cash"))
		self.assertEqual([(d.item_code, flt(d.qty)) for d in pi.items], [("RAW-BUN", 3)])
		self.assertAlmostEqual(flt(pi.grand_total), 15, 2)

	def test_a_cancelled_purchase_is_back_in_the_drawer_and_a_closed_shift_keeps_it(self):
		expected = self.expected_cash()
		first = shift.cash_purchase(json.dumps(BOUGHT), "Cash")
		frappe.set_user("Administrator")
		frappe.get_doc("Purchase Invoice", first).cancel()
		frappe.set_user(CASHIER)
		self.assertAlmostEqual(self.expected_cash(), expected, 2)
		second = shift.cash_purchase(json.dumps(BOUGHT), "Cash")
		rows = shift.closing_data()["rows"]
		shift.close_shift(json.dumps({r["mode_of_payment"]: r["expected_amount"] for r in rows}))
		closed = frappe.get_doc("Resto Shift", self.shift)
		self.assertAlmostEqual(flt(closed.purchases_total), 160, 2)
		self.assertAlmostEqual(flt(next(p for p in closed.payments if p.mode_of_payment == "Cash").purchases_amount), 160, 2)
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Purchase Invoice", second).cancel)
