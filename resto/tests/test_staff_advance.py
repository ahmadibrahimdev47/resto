"""Staff advances from the drawer (D-237, resto/resto/staff_advance.py): owed by the worker until taken
off their salary, so not an expense. The ledger and the drawer must stay exact. Run only on a
throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_staff_advance
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_years, flt, nowdate

from resto.resto import approval, shift, staff_advance
from resto.resto.setup_check import SetupCheck
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import CASHIER, MANAGER_USER, WAITER_USER, _manager, assert_parts_make_expected

KITCHEN_USER = "kitchen@resto.test"


def advances_account():
	group = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Asset", "is_group": 1, "account_name": "Current Assets"})
	return fx.account("Staff Advances", group, account_type="Receivable")


def worker(first_name):
	name = frappe.db.get_value("Employee", {"first_name": first_name, "company": fx.COMPANY})
	if name:
		return name
	if not frappe.db.exists("Gender", "Male"):
		frappe.get_doc({"doctype": "Gender", "gender": "Male"}).insert(ignore_permissions=True)
	return (
		frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": first_name,
				"gender": "Male",
				"date_of_birth": add_years(nowdate(), -25),
				"date_of_joining": add_years(nowdate(), -1),
				"company": fx.COMPANY,
				"status": "Active",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def cash_account():
	return frappe.db.get_value("Mode of Payment Account", {"parent": "Cash", "company": fx.COMPANY}, "default_account")


class TestStaffAdvance(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		cls.account = advances_account()
		cls.ali = worker("Advance Ali")
		cls.omar = worker("Advance Omar")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		fields = ("require_pos_shift", "blind_shift_count", "staff_advance_account", "staff_advance_cap", "self_advance_needs_manager")
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in fields}
		frappe.db.set_single_value(
			"Resto Settings", {"require_pos_shift": 0, "blind_shift_count": 0, "staff_advance_account": self.account, "staff_advance_cap": 0, "self_advance_needs_manager": 1}
		)
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

	def cash_row(self):
		return next(r for r in shift.closing_data()["rows"] if r["mode_of_payment"] == "Cash")

	def owed(self, employee):
		rows = staff_advance.open_advances(fx.COMPANY, self.account)
		return next((r.owed for r in rows if r.employee == employee), 0)

	def test_paid_from_the_drawer_owed_by_the_worker_not_an_expense(self):
		before = self.owed(self.ali)
		je = staff_advance.add_advance(self.ali, 120, "Cash", "for rent", "req-adv-1")
		doc = frappe.get_doc("Journal Entry", je)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual((doc.custom_remark, self.shift in doc.remark), (1, True))
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Journal Entry", "voucher_no": je, "is_cancelled": 0},
			fields=["account", "debit", "credit", "party_type", "party"],
		)
		by = {g.account: g for g in gl}
		self.assertEqual(len(gl), 2)
		self.assertAlmostEqual(flt(by[self.account].debit), 120, 2)
		self.assertEqual((by[self.account].party_type, by[self.account].party), ("Employee", self.ali))
		self.assertAlmostEqual(flt(by[cash_account()].credit), 120, 2)
		# No expense account moved: it is owed, not spent.
		self.assertFalse(
			[g for g in gl if frappe.get_cached_value("Account", g.account, "root_type") == "Expense"]
		)
		self.assertAlmostEqual(self.owed(self.ali) - before, 120, 2)

	def test_the_drawer_and_the_shift_count_it_apart(self):
		staff_advance.add_advance(self.ali, 100, "Cash", None, "req-adv-2")
		staff_advance.add_advance(self.omar, 50, "Cash", None, "req-adv-3")
		data = shift.closing_data()
		cash = next(r for r in data["rows"] if r["mode_of_payment"] == "Cash")
		self.assertAlmostEqual(cash["expected_amount"], 350, 2)
		self.assertAlmostEqual(cash["advances_amount"], 150, 2)
		self.assertAlmostEqual(cash["expenses_amount"], 0, 2)
		assert_parts_make_expected(self, data["rows"])
		s = data["summary"]
		self.assertEqual(s["advances_count"], 2)
		self.assertAlmostEqual(s["advances_total"], 150, 2)
		self.assertEqual(s["expenses_count"], 0)
		self.assertAlmostEqual(s["net_cash"], s["collected"] - 150, 2)
		self.assertEqual({a["employee"] for a in data["advances"]}, {self.ali, self.omar})
		# More than the drawer holds is refused.
		self.assertRaises(frappe.ValidationError, staff_advance.add_advance, self.ali, 351, "Cash")
		# Closed: the shift keeps it, the print shows it, and its entry cannot be cancelled.
		result = shift.close_shift(json.dumps({r["mode_of_payment"]: r["expected_amount"] for r in data["rows"]}))
		doc = frappe.get_doc("Resto Shift", result["closing"])
		self.assertAlmostEqual(flt(doc.advances_total), 150, 2)
		self.assertAlmostEqual(flt(next(p for p in doc.payments if p.mode_of_payment == "Cash").advances_amount), 150, 2)
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", doc.advances[0].journal_entry).cancel)
		html = frappe.get_print("Resto Shift", doc.name, "Resto Shift Close")
		self.assertIn("Advance Omar", html)

	def test_a_retried_click_pays_once(self):
		first = staff_advance.add_advance(self.ali, 40, "Cash", None, "req-adv-same")
		again = staff_advance.add_advance(self.ali, 40, "Cash", None, "req-adv-same")
		self.assertEqual(first, again)
		self.assertAlmostEqual(self.cash_row()["advances_amount"], 40, 2)

	def test_above_the_limit_a_manager_approves(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "staff_advance_cap", 100)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		self.assertEqual(staff_advance.advance_data()["approval_above"], 100)
		staff_advance.add_advance(self.ali, 100, "Cash", None, "req-cap-1")  # at the limit: no PIN
		self.assertRaises(approval.ApprovalRequired, staff_advance.add_advance, self.ali, 150, "Cash", None, "req-cap-2")
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user(CASHIER)
		je = staff_advance.add_advance(self.ali, 150, "Cash", None, "req-cap-2", MANAGER_USER, "4321")
		row = next(a for a in frappe.get_doc("Resto Shift", self.shift).advances if a.journal_entry == je)
		self.assertEqual(row.approved_by, MANAGER_USER)

	def test_a_cashiers_own_advance_needs_a_manager(self):
		frappe.set_user("Administrator")
		own = worker("Advance Self Cashier")
		frappe.db.set_value("Employee", own, "user_id", CASHIER)
		self.addCleanup(frappe.db.set_value, "Employee", own, "user_id", None)
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user(CASHIER)
		# On (the default): their own, whatever the amount, needs the PIN; a wrong one is refused.
		self.assertEqual(staff_advance.advance_data()["own"], [own])
		self.assertRaises(approval.ApprovalRequired, staff_advance.add_advance, own, 10, "Cash", None, "req-self-1")
		self.assertRaises(approval.ApprovalRequired, staff_advance.add_advance, own, 10, "Cash", None, "req-self-1", MANAGER_USER, "9999")
		je = staff_advance.add_advance(own, 10, "Cash", None, "req-self-1", MANAGER_USER, "4321")
		row = next(a for a in frappe.get_doc("Resto Shift", self.shift).advances if a.journal_entry == je)
		self.assertEqual(row.approved_by, MANAGER_USER)
		# Another worker: no PIN under the limit.
		self.assertTrue(staff_advance.add_advance(self.ali, 11, "Cash", None, "req-self-2"))
		# Off: only the limit counts.
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "self_advance_needs_manager", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.flags.pop("resto_approvals", None)
		frappe.set_user(CASHIER)
		self.assertEqual(staff_advance.advance_data()["own"], [])
		self.assertTrue(staff_advance.add_advance(own, 12, "Cash", None, "req-self-3"))

	def test_only_a_cashier_or_a_manager_pays_one(self):
		for user in (WAITER_USER, KITCHEN_USER):
			frappe.set_user(user)
			with self.subTest(user=user):
				self.assertRaises(frappe.PermissionError, staff_advance.advance_data)
				self.assertRaises(frappe.PermissionError, staff_advance.add_advance, self.ali, 10, "Cash")
		frappe.set_user(CASHIER)
		self.assertFalse(frappe.get_doc("Resto Shift", self.shift).advances)

	def test_the_salary_entry_takes_it_off(self):
		staff_advance.add_advance(self.omar, 80, "Cash", None, "req-adv-salary")
		owed = self.owed(self.omar)
		self.assertGreaterEqual(owed, 80)
		# The month's salary entry: the advance credited back for the worker.
		frappe.set_user("Administrator")
		group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
		expense = fx.account("Salaries Test", group, account_type="Expense Account")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": fx.COMPANY,
				"posting_date": nowdate(),
				"accounts": [
					{"account": expense, "debit_in_account_currency": owed, "cost_center": frappe.get_cached_value("Company", fx.COMPANY, "cost_center")},
					{"account": self.account, "party_type": "Employee", "party": self.omar, "credit_in_account_currency": owed},
				],
			}
		).insert(ignore_permissions=True)
		je.submit()
		self.assertAlmostEqual(self.owed(self.omar), 0, 2)
		from resto.resto.report.resto_open_staff_advances.resto_open_staff_advances import execute

		self.assertNotIn(self.omar, [r.employee for r in execute({})[1]])
		self.assertIn(self.omar, [r.employee for r in execute({"show_settled": 1})[1]])

	def test_the_account_must_be_set_and_receivable(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "staff_advance_account", None)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		self.assertRaises(frappe.ValidationError, staff_advance.advance_data)
		self.assertRaises(frappe.ValidationError, staff_advance.add_advance, self.ali, 10, "Cash")
		frappe.set_user("Administrator")
		self.assertTrue(any(r["area"] == "Staff Advances" and r["level"] == "warning" for r in SetupCheck().run()))
		group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
		self.assertTrue(staff_advance.account_problem(fx.account("Not An Advance", group, account_type="Expense Account"), fx.COMPANY))
		self.assertIsNone(staff_advance.account_problem(self.account, fx.COMPANY))
