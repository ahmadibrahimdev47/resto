"""The daily profit and loss (D-289) ties to the general ledger: with nothing spread its net profit
is the ledger's income less expenses for the day, its lines add up from gross sales down, and its
cost of goods sold view adds up to the cost the sales invoices posted.

bench --site test.resto run-tests --module resto.tests.test_daily_pnl
"""

import json

import frappe
from frappe.desk.query_report import run
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, get_first_day, get_last_day, nowdate

from resto.resto import floor
from resto.setup import install
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import MANAGER_USER, _manager

REPORT = "Resto Daily Profit and Loss"


def ledger_profit(day):
	return flt(
		frappe.db.sql(
			"""select sum(gle.credit - gle.debit) from `tabGL Entry` gle join `tabAccount` acc on acc.name = gle.account
			where gle.company = %s and gle.posting_date = %s and gle.is_cancelled = 0
				and gle.voucher_type != 'Period Closing Voucher' and acc.root_type in ('Income', 'Expense')""",
			(fx.COMPANY, day),
		)[0][0]
	)


def invoice_cost(day):
	"""The cost the day's sales invoices posted to the company's cost of goods sold account."""
	account = frappe.get_cached_value("Company", fx.COMPANY, "default_expense_account")
	return flt(
		frappe.db.sql(
			"""select sum(debit - credit) from `tabGL Entry` where company = %s and posting_date = %s and is_cancelled = 0
			and voucher_type = 'Sales Invoice' and account = %s""",
			(fx.COMPANY, day, account),
		)[0][0]
	)


class TestDailyProfitAndLoss(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		install.grant_standard_permissions()  # as a migrate does
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		self.today = nowdate()
		self.je = None

	def tearDown(self):
		frappe.set_user("Administrator")
		if self.je:
			self.je.reload()
			self.je.cancel()
		frappe.db.commit()

	def sell(self):
		order = floor.create_order(order_type="Takeaway")
		floor.add_items(
			order,
			json.dumps([
				{"item_code": "DISH-BURGER", "qty": 2, "modifiers": [{"group": "Doneness", "option": "Medium"}]},
				{"item_code": "DRK-COLA", "qty": 1},
			]),
		)
		floor.send_to_kitchen(order)
		bill = floor.get_bill(order)
		floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))

	def pay_rent(self, amount):
		indirect = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
		self.rent = fx.account("Shop Rent", indirect, account_type="Expense Account")
		cash = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_type": "Cash", "is_group": 0})
		cost_center = frappe.get_cached_value("Company", fx.COMPANY, "cost_center")
		self.je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": fx.COMPANY,
				"posting_date": self.today,
				"accounts": [
					{"account": self.rent, "debit_in_account_currency": amount, "cost_center": cost_center},
					{"account": cash, "credit_in_account_currency": amount, "cost_center": cost_center},
				],
			}
		)
		self.je.insert(ignore_permissions=True)
		self.je.submit()

	def report(self, **filters):
		frappe.set_user(MANAGER_USER)  # the restaurant manager opens it
		try:
			out = run(REPORT, filters={"company": fx.COMPANY, "from_date": self.today, "to_date": self.today, **filters}, ignore_prepared_report=True)
		finally:
			frappe.set_user("Administrator")
		return [r for r in out["result"] if isinstance(r, dict)]

	def test_net_profit_is_the_ledgers_and_the_lines_add_up(self):
		self.sell()
		self.pay_rent(3000)
		rows = self.report()
		by_key = {}
		for r in rows:
			by_key.setdefault(r["key"], r)
		self.assertAlmostEqual(flt(by_key["net_profit"]["amount"]), ledger_profit(self.today), 2)

		amount = lambda k: flt(by_key[k]["amount"]) if k in by_key else 0  # noqa: E731
		self.assertAlmostEqual(
			amount("gross_sales") - amount("discounts") - amount("taxes") - amount("returns"), amount("net_sales"), 2
		)
		self.assertAlmostEqual(
			amount("net_sales") - amount("food") - amount("packaging") - amount("direct"), amount("gross_profit"), 2
		)
		self.assertAlmostEqual(
			amount("gross_profit") + amount("other_income") - amount("indirect"), amount("net_profit"), 2
		)
		self.assertAlmostEqual(amount("food") + amount("packaging"), invoice_cost(self.today), 2)
		self.assertAlmostEqual(flt(by_key["net_sales"]["pct"]), 100, 2)
		rent = next(r for r in rows if r.get("account") == self.rent)
		self.assertEqual((rent["key"], flt(rent["amount"])), ("indirect_account", 3000))

		# Spread: the rent (fixed by its name, nothing marked in Resto Settings) is the month's rent
		# over the days of the month, and says so.
		frappe.db.delete("Resto Expense Account", {"parent": "Resto Settings", "parentfield": "fixed_expense_accounts"})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		month = flt(
			frappe.db.sql(
				"""select sum(debit - credit) from `tabGL Entry` where account = %s and is_cancelled = 0
				and posting_date between %s and %s""",
				(self.rent, get_first_day(self.today), get_last_day(self.today)),
			)[0][0]
		)
		days = (get_last_day(self.today) - get_first_day(self.today)).days + 1
		spread = next(r for r in self.report(spread_fixed=1) if r.get("account") == self.rent)
		self.assertAlmostEqual(flt(spread["amount"]), month / days, 2)
		self.assertIn("(spread)", spread["label"])

	def test_cost_of_goods_sold_view_is_the_invoices_cost(self):
		self.sell()
		rows = self.report(view="Cost of Goods Sold")
		items = [r for r in rows if r.get("item_code")]
		self.assertTrue(items)
		total = next(r for r in rows if r.get("bold"))
		self.assertAlmostEqual(flt(total["cost"]), sum(flt(r["cost"]) for r in items), 2)
		# The stock ledger's cost per row and the ledger's per invoice differ by rounding at most.
		rows_sold = frappe.db.count("Stock Ledger Entry", {"voucher_type": "Sales Invoice", "posting_date": self.today, "is_cancelled": 0})
		self.assertAlmostEqual(flt(total["cost"]), invoice_cost(self.today), delta=0.01 * rows_sold + 0.01)
		statement = {r["key"]: r for r in self.report()}
		self.assertAlmostEqual(
			flt(total["cost"]), flt(statement["food"]["amount"]) + flt(statement["packaging"]["amount"]), delta=0.01 * rows_sold + 0.01
		)
		for r in items:
			if flt(r["qty"]):
				self.assertAlmostEqual(flt(r["unit_cost"]) * flt(r["qty"]), flt(r["cost"]), 2)
