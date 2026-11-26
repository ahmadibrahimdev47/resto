"""A cash account taken below zero is warned about or refused (D-257).

bench --site test.resto run-tests --module resto.tests.test_cash_guard
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from resto.resto import cash_guard
from resto.tests import fixtures as fx


def set_mode(value):
	frappe.db.set_single_value("Resto Settings", "negative_cash", value)
	frappe.clear_document_cache("Resto Settings", "Resto Settings")


class TestCashGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Cash In Hand", "is_group": 1})
		cls.safe = fx.account("Guard Safe", group, account_type="Cash")
		cls.other = f"Food Waste - {fx.ABBR}"
		cls.bank = f"Card Clearing - {fx.ABBR}"
		cls.cost_center = frappe.get_cached_value("Company", fx.COMPANY, "cost_center")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self.docs = []
		set_mode(cash_guard.WARN)

	def tearDown(self):
		frappe.set_user("Administrator")
		set_mode(cash_guard.WARN)
		for doc in reversed(self.docs):
			doc.reload()
			if doc.docstatus == 1:
				doc.cancel()
		self.assertAlmostEqual(self.balance(), 0, 2)

	def balance(self, date=None):
		return cash_guard.balance(self.safe, fx.COMPANY, date or add_days(nowdate(), 30))

	def je(self, debit, credit, amount, date=None, submit=True):
		doc = frappe.new_doc("Journal Entry")
		doc.update({"company": fx.COMPANY, "posting_date": date or nowdate(), "voucher_type": "Journal Entry"})
		doc.append("accounts", {"account": debit, "debit_in_account_currency": amount, "cost_center": self.cost_center})
		doc.append("accounts", {"account": credit, "credit_in_account_currency": amount, "cost_center": self.cost_center})
		doc.insert(ignore_permissions=True)
		self.docs.append(doc)
		if submit:
			doc.submit()
		return doc

	def test_warn_only_asks_first_then_posts(self):
		self.je(self.safe, self.other, 100)
		draft = self.je(self.other, self.safe, 150, submit=False)
		got = cash_guard.preview("Journal Entry", draft.name)
		self.assertEqual(got["mode"], cash_guard.WARN)
		self.assertIn(self.safe, got["message"])
		self.assertIn("50", got["message"])
		draft.submit()  # warned, not stopped
		self.assertAlmostEqual(self.balance(), -50, 2)

	def test_block_refuses_the_posting(self):
		set_mode(cash_guard.BLOCK)
		self.je(self.safe, self.other, 100)
		draft = self.je(self.other, self.safe, 150, submit=False)
		self.assertEqual(cash_guard.preview("Journal Entry", draft.name)["mode"], cash_guard.BLOCK)
		frappe.db.savepoint("guard")
		self.assertRaises(frappe.ValidationError, draft.submit)
		frappe.db.rollback(save_point="guard")
		self.assertAlmostEqual(self.balance(), 100, 2)
		# Within what the box holds: posted.
		self.je(self.other, self.safe, 80)
		self.assertAlmostEqual(self.balance(), 20, 2)
		# Nothing to warn about: no preview.
		self.assertIsNone(cash_guard.preview("Journal Entry", self.je(self.other, self.safe, 20, submit=False).name))

	def test_the_balance_is_as_of_the_posting_date(self):
		set_mode(cash_guard.BLOCK)
		self.je(self.safe, self.other, 100)
		# The cash came in today: yesterday the box was empty.
		draft = self.je(self.other, self.safe, 50, date=add_days(nowdate(), -1), submit=False)
		frappe.db.savepoint("guard")
		self.assertRaises(frappe.ValidationError, draft.submit)
		frappe.db.rollback(save_point="guard")
		self.assertAlmostEqual(flt(self.balance()), 100, 2)

	def test_only_cash_accounts_are_watched(self):
		set_mode(cash_guard.BLOCK)
		bank_before = cash_guard.balance(self.bank, fx.COMPANY, nowdate())
		self.je(self.other, self.bank, abs(bank_before) + 1000)
		self.je(self.bank, self.other, abs(bank_before) + 1000)
