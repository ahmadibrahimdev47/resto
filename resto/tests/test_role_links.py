"""Every link of the restaurant sidebar a Resto role sees opens for it (D-223).

A report shows in the sidebar for the roles on the report; it opens only with read and report on its
doctype. A page shows for the roles on the page. A doctype shows when it can be read. Every link opens
in the same tab and keeps the restaurant sidebar (D-232).

bench --site test.resto run-tests --module resto.tests.test_role_links
"""

import json
import os

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto.utils import ALL_ROLES
from resto.tests import fixtures as fx

SIDEBAR = os.path.join(os.path.dirname(__file__), "..", "resto", "sidebar", "resto", "resto.json")


def user_with(role):
	email = f"only-{frappe.scrub(role)}@resto.test"
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": role, "send_welcome_email": 0})
		u.insert(ignore_permissions=True)
		u.add_roles(role)
	return email


def links():
	with open(SIDEBAR) as f:
		return [i for i in json.load(f)["items"] if i.get("type") == "Link"]


class TestRoleLinks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.users = {role: user_with(role) for role in ALL_ROLES}

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_each_report_shown_to_a_role_opens_for_it(self):
		for item in links():
			if item.get("link_type") != "Report":
				continue
			report = frappe.get_doc("Report", item["link_to"])
			shown = {r.role for r in report.roles}
			for role, user in self.users.items():
				if role not in shown:
					continue
				with self.subTest(report=report.name, role=role):
					self.assertTrue(frappe.has_permission(report.ref_doctype, "read", user=user))
					self.assertTrue(frappe.has_permission(report.ref_doctype, "report", user=user))

	def test_each_page_shown_to_a_role_opens_for_it(self):
		from resto.resto import analytics

		roles = {r.role for r in frappe.get_doc("Page", "resto-analytics").roles}
		for role, user in self.users.items():
			frappe.set_user(user)
			with self.subTest(page="resto-analytics", role=role):
				if role in roles:
					try:
						analytics.get_dashboard()
					except frappe.PermissionError:
						self.fail(f"{role} sees the page but cannot open it")
					except Exception:
						pass  # data of the test site, not the right to open it
				else:
					self.assertRaises(frappe.PermissionError, analytics.get_dashboard)
		frappe.set_user("Administrator")

	def test_sales_figures_are_not_shown_to_the_floor_roles(self):
		for name in ("Resto Sales Analysis",):
			shown = {r.role for r in frappe.get_doc("Report", name).roles}
			self.assertFalse(shown & {"Resto Cashier", "Resto Waiter", "Resto Host", "Resto Kitchen"})
		pages = {r.role for r in frappe.get_doc("Page", "resto-analytics").roles}
		self.assertFalse(pages & {"Resto Cashier", "Resto Waiter", "Resto Host", "Resto Kitchen"})

	def test_the_items_link_opens_for_every_role(self):
		item = next(i for i in links() if i.get("label") == "Items")
		self.assertEqual((item["link_type"], item["link_to"]), ("DocType", "Item"))
		for role, user in self.users.items():
			with self.subTest(role=role):
				self.assertTrue(frappe.has_permission("Item", "read", user=user))

	def test_purchase_invoices_only_for_who_can_read_them(self):
		# A DocType link is shown only to a user who can read the doctype, so shown means it opens.
		item = next(i for i in links() if i.get("link_to") == "Purchase Invoice")
		self.assertEqual(item["link_type"], "DocType")
		for role in ("Resto Cashier", "Resto Waiter", "Resto Host", "Resto Kitchen"):
			with self.subTest(role=role):
				self.assertFalse(frappe.has_permission("Purchase Invoice", "read", user=self.users[role]))
		self.assertTrue(frappe.has_permission("Purchase Invoice", "read", user=self.users["Resto Manager"]))
		self.assertTrue(frappe.has_permission("Purchase Invoice", "create", user=user_with("Accounts User")))

	def test_supplier_dues_only_for_who_can_open_the_report(self):
		# A Report link is shown by the report's roles (ERPNext's own), so a floor role never sees it.
		names = [i.get("link_to") for i in links()]
		self.assertEqual(names[names.index("Purchase Invoice") + 1], "Accounts Payable Summary")
		item = next(i for i in links() if i.get("link_to") == "Accounts Payable Summary")
		self.assertEqual(item["link_type"], "Report")
		report = frappe.get_doc("Report", "Accounts Payable Summary")
		shown = {r.role for r in report.roles}
		self.assertFalse(shown & {"Resto Cashier", "Resto Waiter", "Resto Host", "Resto Kitchen"})
		self.assertIn("Accounts User", shown)
		accountant = user_with("Accounts User")
		self.assertTrue(frappe.has_permission(report.ref_doctype, "read", user=accountant))
		self.assertTrue(frappe.has_permission(report.ref_doctype, "report", user=accountant))

	def test_every_link_stays_in_the_restaurant_sidebar(self):
		# v16 opens only a URL link in a new tab, and a URL carries no sidebar, so the desk would put
		# the item's own module around it. A DocType, Report or Page link is routed inside the
		# sidebar showing it: same tab, the restaurant sidebar kept.
		for item in links():
			with self.subTest(link=item.get("label")):
				self.assertNotEqual(item.get("link_type"), "URL")
				self.assertFalse(item.get("open_in_new_tab"))

	def test_each_doctype_link_is_a_doctype(self):
		for item in links():
			if item.get("link_type") == "DocType":
				self.assertTrue(frappe.db.exists("DocType", item["link_to"]), item["link_to"])


class TestShiftVisibility(IntegrationTestCase):
	"""A cashier reads only their own shifts (the shift's owner is its cashier: it is opened by them);
	the manager reads every cashier's shift (D-223)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.a = user_with_email("cashier-a@resto.test", "Resto Cashier")
		cls.b = user_with_email("cashier-b@resto.test", "Resto Cashier")
		cls.manager = user_with("Resto Manager")

	def shift_of(self, user):
		doc = frappe.new_doc("Resto Shift")
		doc.update({"user": user, "status": "Open"})
		doc.owner = user
		return doc

	def test_a_cashier_reads_only_their_own_shift(self):
		mine, theirs = self.shift_of(self.a), self.shift_of(self.b)
		self.assertTrue(frappe.has_permission("Resto Shift", "read", doc=mine, user=self.a))
		self.assertFalse(frappe.has_permission("Resto Shift", "read", doc=theirs, user=self.a))
		for doc in (mine, theirs):
			self.assertTrue(frappe.has_permission("Resto Shift", "read", doc=doc, user=self.manager))

	def test_a_cashier_cannot_change_a_shift_by_hand(self):
		self.assertFalse(frappe.has_permission("Resto Shift", "write", doc=self.shift_of(self.a), user=self.a))


def user_with_email(email, role):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": role, "send_welcome_email": 0})
		u.insert(ignore_permissions=True)
		u.add_roles(role)
	return email


class TestAccountingSummaryLinks(IntegrationTestCase):
	"""Each summary report follows its detailed report in ERPNext's sidebars, once (D-228)."""

	def test_each_summary_follows_its_report_once(self):
		from resto.setup import install

		install.add_accounting_summary_links()
		install.add_accounting_summary_links()  # again: nothing more
		doctype = install._sidebar_doctype()
		child = frappe.get_meta(doctype).get_field("items").options
		for after, report in install.SUMMARY_LINKS:
			if not frappe.db.exists("Report", report):
				continue
			for anchor in frappe.get_all(child, filters={"parenttype": doctype, "link_type": "Report", "link_to": after}, fields=["parent", "idx"]):
				with self.subTest(sidebar=anchor.parent, report=report):
					rows = frappe.get_all(child, filters={"parent": anchor.parent, "parenttype": doctype, "link_to": report}, fields=["idx"])
					self.assertEqual([r.idx for r in rows], [anchor.idx + 1])

