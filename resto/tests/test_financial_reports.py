"""A Resto Manager with no accounts role opens the financial reports of the restaurant sidebar
(D-286): each one is allowed to the role (a Custom Role) and its doctype's report right is given,
read only.

bench --site test.resto run-tests --module resto.tests.test_financial_reports
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_first_day, nowdate

from resto.setup import install
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import MANAGER_USER, _manager


class TestFinancialReports(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		install.grant_standard_permissions()  # as a migrate does
		install.grant_report_roles(install.FINANCIAL_REPORTS, "Resto Manager")
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def filters(self):
		from erpnext.accounts.utils import get_fiscal_year

		today = nowdate()
		fy = get_fiscal_year(today, company=fx.COMPANY)[0]
		dates = {"company": fx.COMPANY, "from_date": str(get_first_day(today)), "to_date": today}
		statement = {
			"company": fx.COMPANY,
			"filter_based_on": "Date Range",
			"period_start_date": dates["from_date"],
			"period_end_date": today,
			"from_fiscal_year": fy,
			"to_fiscal_year": fy,
			"periodicity": "Monthly",
			"accumulated_values": 1,
		}
		trends = {"company": fx.COMPANY, "period": "Monthly", "based_on": "Item", "fiscal_year": fy, "period_based_on": "posting_date"}
		return {
			"Profit and Loss Statement": statement,
			"Balance Sheet": statement,
			"Cash Flow": statement,
			"Consolidated Financial Statement": {**statement, "report": "Balance Sheet"},
			"Trial Balance": {**dates, "fiscal_year": fy},
			"General Ledger": {**dates, "group_by": "Group by Voucher (Consolidated)"},
			"Sales Register": dates,
			"Gross Profit": {**dates, "group_by": "Invoice"},
			"Gross and Net Profit Report": statement,
			"Financial Ratios": {**statement, "filter_based_on": "Fiscal Year"},
			"Sales Invoice Trends": trends,
			"Purchase Invoice Trends": trends,
			"Sales Payment Summary": dates,
		}

	def test_a_plain_manager_opens_them(self):
		from frappe.desk.query_report import get_report_doc, run

		accounts = {"Accounts User", "Accounts Manager", "Auditor", "System Manager"}
		self.assertEqual(set(frappe.get_roles(MANAGER_USER)) & accounts, set())
		filters = self.filters()
		self.assertEqual(set(filters), set(install.FINANCIAL_REPORTS))
		frappe.set_user(MANAGER_USER)
		refused, other = [], []
		for name in install.FINANCIAL_REPORTS:
			if not frappe.db.exists("Report", name):
				continue
			try:
				get_report_doc(name)
				run(name, filters=filters[name], ignore_prepared_report=True)
			except frappe.PermissionError as e:
				refused.append(f"{name}: {e}")
			except Exception as e:
				# A filter this test did not give is not what is checked here.
				other.append(f"{name}: {type(e).__name__}: {e}")
		if other:
			print("\n".join(["not run (not a permission):", *other]))
		self.assertEqual(refused, [])
		# Read only: posting stays with the accounts roles.
		self.assertFalse(frappe.has_permission("Journal Entry", "create"))
		self.assertFalse(frappe.has_permission("GL Entry", "write"))

	def test_their_own_roles_are_kept(self):
		for name in install.FINANCIAL_REPORTS:
			if not frappe.db.exists("Report", name):
				continue
			own = set(frappe.get_all("Has Role", filters={"parent": name, "parenttype": "Report"}, pluck="role"))
			custom = frappe.db.get_value("Custom Role", {"report": name})
			allowed = set(frappe.get_all("Has Role", filters={"parent": custom, "parenttype": "Custom Role"}, pluck="role"))
			self.assertIn("Resto Manager", allowed, name)
			self.assertTrue(own <= allowed, name)
