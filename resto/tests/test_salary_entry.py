"""The month's salary entry (D-249, resto/resto/doctype/resto_salary_entry): one Journal Entry for the
salaries, the advances taken off and the net paid. The ledger must stay exact. Run only on a
throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_salary_entry
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from resto.resto import staff_advance
from resto.resto.doctype.resto_salary_entry import resto_salary_entry as salaries
from resto.resto.setup_check import SetupCheck
from resto.tests import fixtures as fx
from resto.tests.test_drawer_cash import safe_account
from resto.tests.test_floor_customers_shift import MANAGER_USER, _manager
from resto.tests.test_role_links import FLOOR_ROLES, user_with
from resto.tests.test_staff_advance import advances_account, worker

FIELDS = ("staff_advance_account", "salary_account", "main_safe_account")


def salary_expense_account():
	group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
	return fx.account("Salaries Entry Test", group, account_type="Expense Account")


def give_advance(employee, amount, advances, cash):
	cost_center = frappe.get_cached_value("Company", fx.COMPANY, "cost_center")
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": fx.COMPANY,
			"posting_date": nowdate(),
			"accounts": [
				{"account": advances, "party_type": "Employee", "party": employee, "debit_in_account_currency": amount, "cost_center": cost_center},
				{"account": cash, "credit_in_account_currency": amount, "cost_center": cost_center},
			],
		}
	).insert(ignore_permissions=True)
	je.submit()


def gl(je):
	return frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Journal Entry", "voucher_no": je, "is_cancelled": 0},
		fields=["account", "debit", "credit", "party_type", "party"],
	)


class TestSalaryEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		cls.advances = advances_account()
		cls.safe = safe_account()
		cls.expense = salary_expense_account()
		cls.amr = worker("Salary Amr")
		cls.sami = worker("Salary Sami")
		frappe.db.set_value("Employee", cls.amr, "resto_monthly_salary", 3000)
		frappe.db.set_value("Employee", cls.sami, "resto_monthly_salary", 2000)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in FIELDS}
		frappe.db.set_single_value(
			"Resto Settings", {"staff_advance_account": self.advances, "salary_account": self.expense, "main_safe_account": self.safe}
		)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", self._kept)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def owed(self, employee):
		rows = staff_advance.open_advances(fx.COMPANY, self.advances)
		return next((flt(r.owed) for r in rows if r.employee == employee), 0)

	def fresh_month(self, month):
		"""No entry posted for the month yet (a run before this one may have left one)."""
		for name in frappe.get_all("Resto Salary Entry", filters={"company": fx.COMPANY, "salary_month": month, "docstatus": 1}, pluck="name"):
			frappe.get_doc("Resto Salary Entry", name).cancel()

	def entry(self, month, rows):
		frappe.set_user(MANAGER_USER)
		doc = frappe.new_doc("Resto Salary Entry")
		doc.update({"salary_month": month, "company": fx.COMPANY, "pay_from_account": self.safe})
		for r in rows:
			doc.append("workers", r)
		return doc

	def test_one_entry_for_salaries_advances_taken_off_and_the_net(self):
		self.fresh_month("2031-01-01")
		give_advance(self.amr, 500, self.advances, self.safe)
		give_advance(self.sami, 300, self.advances, self.safe)
		amr_owed, sami_owed = self.owed(self.amr), self.owed(self.sami)
		frappe.set_user(MANAGER_USER)
		got = {r["employee"]: r for r in salaries.get_workers(fx.COMPANY)}
		self.assertAlmostEqual(got[self.amr]["salary"], 3000, 2)
		self.assertAlmostEqual(got[self.amr]["owed"], amr_owed, 2)
		self.assertEqual(got[self.amr]["deduct"], 1)
		# Amr's 500 taken off now; Sami's left owed for a later month.
		doc = self.entry(
			"2031-01-17",
			[
				{"employee": self.amr, "salary": 3000, "deduct": 1, "deduct_amount": 500},
				{"employee": self.sami, "salary": 2000, "deduct": 0, "deduct_amount": 300},
			],
		)
		doc.insert()
		self.assertEqual(str(doc.salary_month), "2031-01-01")
		self.assertEqual((flt(doc.total_salaries), flt(doc.total_deducted), flt(doc.total_net)), (5000, 500, 4500))
		self.assertEqual(flt(doc.workers[1].deduct_amount), 0)
		doc.submit()
		# One row per worker on each side (D-260), with the worker in its remark.
		je = frappe.get_doc("Journal Entry", doc.journal_entry)
		salary_rows = [a for a in je.accounts if a.account == self.expense]
		cash_rows = [a for a in je.accounts if a.account == self.safe]
		self.assertEqual(sorted(flt(a.debit_in_account_currency) for a in salary_rows), [2000, 3000])
		self.assertEqual(sorted(flt(a.credit_in_account_currency) for a in cash_rows), [2000, 2500])
		names = {self.amr: frappe.db.get_value("Employee", self.amr, "employee_name")}
		self.assertTrue(any(names[self.amr] in (a.user_remark or "") and "01-2031" in a.user_remark for a in salary_rows))
		self.assertEqual(len(je.accounts), 5)
		by = {}
		for g in gl(doc.journal_entry):
			key = (g.account, g.party or "")
			if key in by:
				by[key].debit += flt(g.debit)
				by[key].credit += flt(g.credit)
			else:
				by[key] = g
		self.assertAlmostEqual(flt(by[(self.expense, "")].debit), 5000, 2)
		self.assertAlmostEqual(flt(by[(self.advances, self.amr)].credit), 500, 2)
		self.assertEqual(by[(self.advances, self.amr)].party_type, "Employee")
		self.assertNotIn((self.advances, self.sami), by)
		self.assertAlmostEqual(flt(by[(self.safe, "")].credit), 4500, 2)
		self.assertAlmostEqual(self.owed(self.amr), amr_owed - 500, 2)
		self.assertAlmostEqual(self.owed(self.sami), sami_owed, 2)
		self.assertIn("01-2031", frappe.db.get_value("Journal Entry", doc.journal_entry, "remark"))

	def test_never_more_than_owed_nor_the_salary(self):
		give_advance(self.sami, 400, self.advances, self.safe)
		owed = self.owed(self.sami)
		too_much = self.entry("2031-02-01", [{"employee": self.sami, "salary": 5000, "deduct": 1, "deduct_amount": owed + 1}])
		self.assertRaises(frappe.ValidationError, too_much.insert)
		over_salary = self.entry("2031-02-01", [{"employee": self.sami, "salary": 100, "deduct": 1, "deduct_amount": 200}])
		self.assertRaises(frappe.ValidationError, over_salary.insert)
		twice = self.entry("2031-02-01", [{"employee": self.amr, "salary": 3000}, {"employee": self.amr, "salary": 3000}])
		self.assertRaises(frappe.ValidationError, twice.insert)
		nothing = self.entry("2031-02-01", [{"employee": self.amr, "salary": 0}])
		self.assertRaises(frappe.ValidationError, nothing.insert)

	def test_a_month_is_posted_once(self):
		self.fresh_month("2031-03-01")
		first = self.entry("2031-03-05", [{"employee": self.amr, "salary": 3000, "deduct": 0}])
		first.insert()
		first.submit()
		second = self.entry("2031-03-20", [{"employee": self.amr, "salary": 3000, "deduct": 0}])
		second.insert()  # a draft, with a warning
		self.assertRaises(frappe.ValidationError, second.submit)
		# Its Journal Entry goes only with the salary entry.
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", first.journal_entry).cancel)
		frappe.set_user(MANAGER_USER)
		first.reload()
		first.cancel()
		self.assertEqual(frappe.db.get_value("Journal Entry", first.journal_entry, "docstatus"), 2)
		second.reload()
		second.submit()
		self.assertEqual(frappe.db.get_value("Journal Entry", second.journal_entry, "docstatus"), 1)

	def test_only_the_managers_and_the_salary_stays_out_of_sight(self):
		for role in FLOOR_ROLES:
			user = user_with(role)
			with self.subTest(role=role):
				self.assertFalse(frappe.has_permission("Resto Salary Entry", "read", user=user))
				frappe.set_user(user)
				self.assertRaises(frappe.PermissionError, salaries.get_workers, fx.COMPANY)
				self.assertRaises(frappe.PermissionError, salaries.new_entry_defaults)
				frappe.set_user("Administrator")
		for right in ("read", "create", "submit", "cancel"):
			self.assertTrue(frappe.has_permission("Resto Salary Entry", right, user=MANAGER_USER), right)
		self.assertTrue(frappe.has_permission("Resto Salary Entry", "read", user=user_with("Accounts User")))
		self.assertEqual(frappe.get_meta("Employee").get_field("resto_monthly_salary").permlevel, 1)
		# The manager reads and sets it on the worker's record.
		frappe.set_user(MANAGER_USER)
		doc = frappe.get_doc("Employee", self.amr)
		doc.apply_fieldlevel_read_permissions()
		self.assertAlmostEqual(flt(doc.get("resto_monthly_salary")), 3000, 2)

	def test_the_accounts_are_checked(self):
		self.assertIsNone(salaries.salary_account_problem(self.expense, fx.COMPANY))
		self.assertTrue(salaries.salary_account_problem(self.safe, fx.COMPANY))
		self.assertIsNone(salaries.pay_from_problem(self.safe, fx.COMPANY))
		self.assertTrue(salaries.pay_from_problem(self.expense, fx.COMPANY))
		wrong_from = self.entry("2031-04-01", [{"employee": self.amr, "salary": 3000}])
		wrong_from.pay_from_account = self.expense
		self.assertRaises(frappe.ValidationError, wrong_from.insert)
		frappe.set_user("Administrator")
		self.assertTrue(any(r["area"] == "Monthly Salaries" and r["level"] in ("ok", "warning") for r in SetupCheck().run()))
		frappe.db.set_single_value("Resto Settings", "salary_account", None)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		self.assertTrue(any(r["area"] == "Monthly Salaries" and r["level"] == "warning" for r in SetupCheck().run()))
		no_account = self.entry("2031-04-01", [{"employee": self.amr, "salary": 3000}])
		self.assertRaises(frappe.ValidationError, no_account.insert)
