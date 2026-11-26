"""Cash put into the drawer and cash dropped to the main safe (D-245, resto/resto/drawer_cash.py): each a
Journal Entry the shift counts on its own line. The ledger and the drawer must stay exact. Run only on
a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_drawer_cash
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, flt, now_datetime

from resto.resto import approval, drawer_cash, shift, staff_advance
from resto.resto.setup_check import SetupCheck
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import CASHIER, MANAGER_USER, WAITER_USER, _manager, assert_parts_make_expected
from resto.tests.test_staff_advance import KITCHEN_USER, advances_account, cash_account, worker

PIN = "4321"
FIELDS = ("require_pos_shift", "blind_shift_count", "staff_advance_account", "staff_advance_cap", "main_safe_account")


def safe_account():
	group = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Asset", "is_group": 1, "account_name": "Current Assets"})
	return fx.account("Main Safe Test", group, account_type="Cash")


def other_account():
	group = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Equity", "is_group": 1})
	return fx.account("Cash Top Up Test", group)


def set_other_accounts(accounts):
	"""Drawer Cash In Accounts of Resto Settings, written straight (the test sets a wrong one too)."""
	frappe.db.delete("Resto Expense Account", {"parent": "Resto Settings", "parentfield": "drawer_cash_in_accounts"})
	for i, account in enumerate(accounts, 1):
		frappe.get_doc(
			{
				"doctype": "Resto Expense Account",
				"parent": "Resto Settings",
				"parenttype": "Resto Settings",
				"parentfield": "drawer_cash_in_accounts",
				"idx": i,
				"account": account,
			}
		).db_insert()
	frappe.clear_document_cache("Resto Settings", "Resto Settings")


def gl(je):
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Journal Entry", "voucher_no": je, "is_cancelled": 0},
		fields=["account", "debit", "credit", "party_type", "party"],
	)
	return {g.account: g for g in rows}, len(rows)


class TestDrawerCash(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		cls.advances = advances_account()
		cls.safe = safe_account()
		cls.other = other_account()
		cls.hassan = worker("Repay Hassan")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in FIELDS}
		self._kept_others = frappe.get_all(
			"Resto Expense Account",
			filters={"parent": "Resto Settings", "parentfield": "drawer_cash_in_accounts"},
			order_by="idx",
			pluck="account",
		)
		frappe.db.set_single_value(
			"Resto Settings",
			{"require_pos_shift": 0, "blind_shift_count": 0, "staff_advance_account": self.advances, "staff_advance_cap": 0, "main_safe_account": self.safe},
		)
		set_other_accounts([self.other])
		if frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile"}) and not frappe.db.exists(
			"POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}
		):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin(PIN)
		frappe.set_user("Administrator")
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.set_user(CASHIER)
		self.shift = shift.open_shift(json.dumps({"Cash": 500}))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", self._kept)
		set_other_accounts(self._kept_others)
		frappe.flags.pop("resto_approvals", None)

	def cash_row(self, data=None):
		return next(r for r in (data or shift.closing_data())["rows"] if r["mode_of_payment"] == "Cash")

	def owed(self, employee):
		rows = staff_advance.open_advances(fx.COMPANY, self.advances)
		return next((flt(r.owed) for r in rows if r.employee == employee), 0)

	def move(self, je):
		return next(m for m in frappe.get_doc("Resto Shift", self.shift).cash_moves if m.journal_entry == je)

	# ------------------------------------------------------------------ cash in

	def test_an_advance_paid_back_goes_into_the_drawer_and_off_what_is_owed(self):
		staff_advance.add_advance(self.hassan, 100, "Cash", None, "req-dc-adv")
		owed = self.owed(self.hassan)
		self.assertGreaterEqual(owed, 100)
		# No PIN: it is the worker's own money coming back, up to what they owe.
		je = drawer_cash.add_cash_in(drawer_cash.ADVANCE, 60, "Cash", employee=self.hassan, request_id="req-dc-repay")
		by, count = gl(je)
		self.assertEqual(count, 2)
		self.assertAlmostEqual(flt(by[cash_account()].debit), 60, 2)
		self.assertAlmostEqual(flt(by[self.advances].credit), 60, 2)
		self.assertEqual((by[self.advances].party_type, by[self.advances].party), ("Employee", self.hassan))
		self.assertAlmostEqual(self.owed(self.hassan), owed - 60, 2)
		row = self.move(je)
		self.assertEqual((row.move, row.reason, row.employee, row.employee_name), (drawer_cash.CASH_IN, drawer_cash.ADVANCE, self.hassan, "Repay Hassan"))
		# More than is still owed is refused.
		self.assertRaises(
			frappe.ValidationError, drawer_cash.add_cash_in, drawer_cash.ADVANCE, owed - 60 + 1, "Cash", employee=self.hassan
		)
		data = shift.closing_data()
		cash = self.cash_row(data)
		self.assertAlmostEqual(cash["expected_amount"], 500 - 100 + 60, 2)
		self.assertAlmostEqual(cash["cash_in_amount"], 60, 2)
		assert_parts_make_expected(self, data["rows"])
		# The dialog lists only workers who owe.
		workers = {w["employee"]: w["owed"] for w in drawer_cash.cash_in_data()["workers"]}
		self.assertAlmostEqual(workers.get(self.hassan, 0), owed - 60, 2)

	def test_change_from_the_main_safe_needs_the_managers_pin(self):
		self.assertRaises(approval.ApprovalRequired, drawer_cash.add_cash_in, drawer_cash.CHANGE, 70, "Cash")
		je = drawer_cash.add_cash_in(drawer_cash.CHANGE, 70, "Cash", request_id="req-dc-change", approver=MANAGER_USER, pin=PIN)
		by, count = gl(je)
		self.assertEqual(count, 2)
		self.assertAlmostEqual(flt(by[cash_account()].debit), 70, 2)
		self.assertAlmostEqual(flt(by[self.safe].credit), 70, 2)
		self.assertEqual(self.move(je).approved_by, MANAGER_USER)
		self.assertAlmostEqual(self.cash_row()["expected_amount"], 570, 2)

	def test_another_reason_only_from_an_allowed_account_with_a_note(self):
		group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
		elsewhere = fx.account("Not A Cash In Account", group, account_type="Expense Account")
		ok = {"approver": MANAGER_USER, "pin": PIN}
		self.assertRaises(frappe.ValidationError, drawer_cash.add_cash_in, drawer_cash.OTHER, 30, "Cash", account=elsewhere, remarks="x", **ok)
		self.assertRaises(frappe.ValidationError, drawer_cash.add_cash_in, drawer_cash.OTHER, 30, "Cash", account=self.other, **ok)
		self.assertRaises(approval.ApprovalRequired, drawer_cash.add_cash_in, drawer_cash.OTHER, 30, "Cash", account=self.other, remarks="top up")
		self.assertRaises(frappe.ValidationError, drawer_cash.add_cash_in, "Anything", 30, "Cash", **ok)
		self.assertRaises(frappe.ValidationError, drawer_cash.add_cash_in, drawer_cash.CHANGE, 0, "Cash", **ok)
		je = drawer_cash.add_cash_in(drawer_cash.OTHER, 30, "Cash", account=self.other, remarks="top up", request_id="req-dc-other", **ok)
		by, count = gl(je)
		self.assertEqual(count, 2)
		self.assertAlmostEqual(flt(by[cash_account()].debit), 30, 2)
		self.assertAlmostEqual(flt(by[self.other].credit), 30, 2)
		self.assertEqual(self.move(je).remarks, "top up")
		# The drawer's own account, even when listed by mistake, never.
		frappe.set_user("Administrator")
		set_other_accounts([self.other, cash_account()])
		frappe.set_user(CASHIER)
		self.assertRaises(frappe.ValidationError, drawer_cash.add_cash_in, drawer_cash.OTHER, 30, "Cash", account=cash_account(), remarks="x", **ok)

	# ------------------------------------------------------------------ cash drop

	def test_a_drop_needs_the_pin_and_never_more_than_the_drawer_holds(self):
		ok = {"approver": MANAGER_USER, "pin": PIN}
		self.assertRaises(frappe.ValidationError, drawer_cash.cash_drop, 501, "Cash", **ok)
		self.assertRaises(approval.ApprovalRequired, drawer_cash.cash_drop, 100, "Cash")
		je = drawer_cash.cash_drop(200, "Cash", "to the safe", "req-dc-drop", **ok)
		by, count = gl(je)
		self.assertEqual(count, 2)
		self.assertAlmostEqual(flt(by[self.safe].debit), 200, 2)
		self.assertAlmostEqual(flt(by[cash_account()].credit), 200, 2)
		row = self.move(je)
		self.assertEqual((row.move, row.reason, row.after_close, row.approved_by), (drawer_cash.CASH_DROP, drawer_cash.TO_SAFE, 0, MANAGER_USER))
		cash = self.cash_row()
		self.assertAlmostEqual(cash["expected_amount"], 300, 2)
		self.assertAlmostEqual(cash["drops_amount"], 200, 2)
		# Counting blind: refused without telling what the drawer should hold.
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "blind_shift_count", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.ValidationError) as caught:
			drawer_cash.cash_drop(301, "Cash", **ok)
		self.assertNotIn("300", str(caught.exception))

	def test_a_retried_click_posts_once(self):
		ok = {"approver": MANAGER_USER, "pin": PIN}
		first = drawer_cash.add_cash_in(drawer_cash.CHANGE, 40, "Cash", request_id="req-dc-same-in", **ok)
		again = drawer_cash.add_cash_in(drawer_cash.CHANGE, 40, "Cash", request_id="req-dc-same-in", **ok)
		self.assertEqual(first, again)
		first = drawer_cash.cash_drop(25, "Cash", None, "req-dc-same-drop", **ok)
		again = drawer_cash.cash_drop(25, "Cash", None, "req-dc-same-drop", **ok)
		self.assertEqual(first, again)
		cash = self.cash_row()
		self.assertAlmostEqual(cash["cash_in_amount"], 40, 2)
		self.assertAlmostEqual(cash["drops_amount"], 25, 2)
		self.assertEqual(len(frappe.get_doc("Resto Shift", self.shift).cash_moves), 2)

	# ------------------------------------------------------------------ the close

	def test_the_close_counts_them_apart_and_in_the_net(self):
		ok = {"approver": MANAGER_USER, "pin": PIN}
		drawer_cash.add_cash_in(drawer_cash.CHANGE, 50, "Cash", request_id="req-dc-c1", **ok)
		drawer_cash.add_cash_in(drawer_cash.OTHER, 30, "Cash", account=self.other, remarks="top up", request_id="req-dc-c2", **ok)
		drawer_cash.cash_drop(100, "Cash", None, "req-dc-c3", **ok)
		data = shift.closing_data()
		cash = self.cash_row(data)
		self.assertAlmostEqual(cash["expected_amount"], 480, 2)
		self.assertAlmostEqual(cash["cash_in_amount"], 80, 2)
		self.assertAlmostEqual(cash["drops_amount"], 100, 2)
		assert_parts_make_expected(self, data["rows"])
		s = data["summary"]
		self.assertEqual((s["cash_in_count"], s["drops_count"]), (2, 1))
		self.assertAlmostEqual(s["cash_in_total"], 80, 2)
		self.assertAlmostEqual(s["drops_total"], 100, 2)
		self.assertAlmostEqual(
			s["net_cash"], s["collected"] - s["expenses_total"] - s["purchases_total"] - s["advances_total"] + 80 - 100, 2
		)
		self.assertEqual(len(data["cash_moves"]), 3)
		result = shift.close_shift(json.dumps({r["mode_of_payment"]: r["expected_amount"] for r in data["rows"]}))
		doc = frappe.get_doc("Resto Shift", result["closing"])
		self.assertAlmostEqual(flt(doc.cash_in_total), 80, 2)
		self.assertAlmostEqual(flt(doc.cash_drops_total), 100, 2)
		pay = next(p for p in doc.payments if p.mode_of_payment == "Cash")
		self.assertAlmostEqual(flt(pay.cash_in_amount), 80, 2)
		self.assertAlmostEqual(flt(pay.drops_amount), 100, 2)
		self.assertFalse(doc.over_short_entry)
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", doc.cash_moves[0].journal_entry).cancel)
		html = frappe.get_print("Resto Shift", doc.name, "Resto Shift Close")
		self.assertIn("Cash In and Drops", html)
		self.assertIn("top up", html)

	def test_right_after_the_close_a_drop_takes_from_what_was_counted(self):
		ok = {"approver": MANAGER_USER, "pin": PIN}
		rows = shift.closing_data()["rows"]
		result = shift.close_shift(json.dumps({r["mode_of_payment"]: r["expected_amount"] for r in rows}))
		closed = result["closing"]
		data = drawer_cash.drop_data()
		self.assertEqual((data["shift"], data["after_close"]), (closed, True))
		self.assertAlmostEqual(data["left"]["Cash"], 500, 2)
		self.assertRaises(frappe.ValidationError, drawer_cash.cash_drop, 501, "Cash", **ok)
		self.assertRaises(approval.ApprovalRequired, drawer_cash.cash_drop, 450, "Cash")
		je = drawer_cash.cash_drop(450, "Cash", "end of day", "req-dc-after", **ok)
		by, _count = gl(je)
		self.assertAlmostEqual(flt(by[self.safe].debit), 450, 2)
		self.assertAlmostEqual(flt(by[cash_account()].credit), 450, 2)
		doc = frappe.get_doc("Resto Shift", closed)
		row = next(m for m in doc.cash_moves if m.journal_entry == je)
		self.assertEqual((row.after_close, row.approved_by), (1, MANAGER_USER))
		self.assertAlmostEqual(drawer_cash.drop_data()["left"]["Cash"], 50, 2)
		self.assertRaises(frappe.ValidationError, drawer_cash.cash_drop, 60, "Cash", **ok)
		# What the shift counted stays as counted: the drop is not one of its figures.
		self.assertEqual(shift.compute(doc)["summary"]["drops_count"], 0)
		self.assertAlmostEqual(flt(doc.cash_drops_total), 0, 2)
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", je).cancel)
		html = frappe.get_print("Resto Shift", closed, "Resto Shift Close")
		self.assertIn("After the Close", html)
		# Long after the close: no shift to drop from.
		frappe.db.sql(
			"update `tabResto Shift` set closing_time = %s where user = %s and status = 'Closed'",
			(add_to_date(now_datetime(), hours=-(drawer_cash.AFTER_CLOSE_HOURS + 1)), CASHIER),
		)
		frappe.set_user(CASHIER)
		self.assertRaises(frappe.ValidationError, drawer_cash.drop_data)

	# ------------------------------------------------------------------ who, and the settings

	def test_only_a_cashier_or_a_manager_moves_cash(self):
		for user in (WAITER_USER, KITCHEN_USER):
			frappe.set_user(user)
			with self.subTest(user=user):
				self.assertRaises(frappe.PermissionError, drawer_cash.cash_in_data)
				self.assertRaises(frappe.PermissionError, drawer_cash.add_cash_in, drawer_cash.CHANGE, 10, "Cash")
				self.assertRaises(frappe.PermissionError, drawer_cash.drop_data)
				self.assertRaises(frappe.PermissionError, drawer_cash.cash_drop, 10, "Cash")
		frappe.set_user(CASHIER)
		self.assertFalse(frappe.get_doc("Resto Shift", self.shift).cash_moves)

	def test_the_main_safe_is_its_own_cash_account(self):
		group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
		self.assertTrue(drawer_cash.main_safe_problem(fx.account("Not A Safe", group, account_type="Expense Account"), fx.COMPANY))
		self.assertIsNone(drawer_cash.main_safe_problem(self.safe, fx.COMPANY))
		self.assertRaises(frappe.ValidationError, drawer_cash.main_safe, fx.COMPANY, self.safe)
		self.assertTrue(drawer_cash.other_problem(self.advances, fx.COMPANY))
		self.assertIsNone(drawer_cash.other_problem(self.other, fx.COMPANY))
		frappe.set_user("Administrator")
		self.assertTrue(any(r["area"] == "Main Safe" and r["level"] == "ok" for r in SetupCheck().run()))
		frappe.db.set_single_value("Resto Settings", "main_safe_account", None)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		self.assertTrue(any(r["area"] == "Main Safe" and r["level"] == "warning" for r in SetupCheck().run()))
		frappe.set_user(CASHIER)
		self.assertFalse(drawer_cash.cash_in_data()["safe"])
		self.assertRaises(frappe.ValidationError, drawer_cash.add_cash_in, drawer_cash.CHANGE, 10, "Cash", approver=MANAGER_USER, pin=PIN)
		self.assertRaises(frappe.ValidationError, drawer_cash.cash_drop, 10, "Cash", approver=MANAGER_USER, pin=PIN)
