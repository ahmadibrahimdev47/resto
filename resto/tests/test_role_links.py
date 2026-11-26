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


SETUP = ("Resto Settings", "Resto Hall", "Resto Table", "Resto Kitchen Station", "Resto Modifier Group", "Resto Side Group")
FLOOR_ROLES = ("Resto Cashier", "Resto Waiter", "Resto Host", "Resto Kitchen")


class TestSetupOnlyForTheManagers(IntegrationTestCase):
	"""The floor roles neither change nor see the restaurant's setup (D-233): no Setup section, no
	settings form. They keep select on halls, tables, stations and groups, for the links of an
	order, a reservation and the kitchen screen; the floor and kitchen read the setup on the server."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.users = {role: user_with(role) for role in (*FLOOR_ROLES, "Resto Manager")}

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_the_floor_roles_cannot_open_or_change_the_setup(self):
		for role in FLOOR_ROLES:
			user = self.users[role]
			for doctype in SETUP:
				with self.subTest(role=role, doctype=doctype):
					for right in ("read", "write", "create", "delete", "submit"):
						self.assertFalse(frappe.has_permission(doctype, right, user=user), right)
		for doctype in SETUP:
			with self.subTest(manager=doctype):
				self.assertTrue(frappe.has_permission(doctype, "write", user=self.users["Resto Manager"]))

	def test_the_floor_roles_can_still_pick_a_table_or_a_station(self):
		for role in FLOOR_ROLES:
			for doctype in ("Resto Hall", "Resto Table", "Resto Kitchen Station"):
				with self.subTest(role=role, doctype=doctype):
					self.assertTrue(frappe.has_permission(doctype, "select", user=self.users[role]))

	def test_the_sidebar_shows_the_setup_only_to_the_manager(self):
		for role in FLOOR_ROLES:
			with self.subTest(role=role):
				self.assertFalse(shown_links(self.users[role]) & set(SETUP))
		self.assertTrue(set(SETUP) <= shown_links(self.users["Resto Manager"]))


def shown_links(user):
	from frappe.desk.doctype.sidebar.sidebar import resolve_sidebar

	frappe.set_user(user)
	frappe.clear_cache(user=user)
	sidebar = resolve_sidebar("Resto", user)
	frappe.set_user("Administrator")
	return {i.get("link_to") for i in (sidebar.items if sidebar else []) if i.get("type") == "Link"}


class TestJournalEntriesForTheManager(IntegrationTestCase):
	"""Journal Entries in the restaurant sidebar, after Monthly Salaries, for the manager (D-259)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.users = {role: user_with(role) for role in (*FLOOR_ROLES, "Resto Manager")}

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_the_manager_sees_and_reads_them_the_floor_does_not(self):
		names = [i.get("link_to") for i in links()]
		self.assertEqual(names[names.index("Resto Salary Entry") + 1], "Journal Entry")
		manager = self.users["Resto Manager"]
		self.assertIn("Journal Entry", shown_links(manager))
		self.assertTrue(frappe.has_permission("Journal Entry", "read", user=manager))
		self.assertFalse(frappe.has_permission("Journal Entry", "create", user=manager))
		for role in FLOOR_ROLES:
			with self.subTest(role=role):
				self.assertNotIn("Journal Entry", shown_links(self.users[role]))
				self.assertFalse(frappe.has_permission("Journal Entry", "read", user=self.users[role]))


class TestWorkersForTheManager(IntegrationTestCase):
	"""The manager keeps the workers' records from the restaurant sidebar (D-238): read, write and
	create on Employee, without HR User. The floor roles neither see nor read them; the cashier
	keeps select, for the driver link and the staff advance's worker."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.users = {role: user_with(role) for role in (*FLOOR_ROLES, "Resto Manager")}
		for doctype, field, value in (("Gender", "gender", "Male"), ("Designation", "designation_name", "Cook")):
			if not frappe.db.exists(doctype, value):
				frappe.get_doc({"doctype": doctype, field: value}).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_the_link_opens_the_employee_list_in_the_restaurant_sidebar(self):
		names = [i.get("link_to") for i in links()]
		self.assertEqual(names[names.index("Resto Reservation") + 1], "Employee")
		item = next(i for i in links() if i.get("link_to") == "Employee")
		self.assertEqual((item["label"], item["link_type"]), ("Workers", "DocType"))
		self.assertFalse(item.get("open_in_new_tab"))

	def test_only_the_manager_sees_the_link(self):
		for role in FLOOR_ROLES:
			with self.subTest(role=role):
				self.assertNotIn("Employee", shown_links(self.users[role]))
		self.assertIn("Employee", shown_links(self.users["Resto Manager"]))

	def test_the_floor_roles_cannot_read_a_worker(self):
		for role in FLOOR_ROLES:
			user = self.users[role]
			for right in ("read", "write", "create", "delete", "export"):
				with self.subTest(role=role, right=right):
					self.assertFalse(frappe.has_permission("Employee", right, user=user))
		self.assertTrue(frappe.has_permission("Employee", "select", user=self.users["Resto Cashier"]))

	def test_the_manager_adds_and_edits_a_worker_but_cannot_delete_one(self):
		manager = self.users["Resto Manager"]
		for right in ("read", "write", "create"):
			self.assertTrue(frappe.has_permission("Employee", right, user=manager), right)
		self.assertFalse(frappe.has_permission("Employee", "delete", user=manager))
		for doctype in ("Gender", "Designation"):
			self.assertTrue(frappe.has_permission(doctype, "select", user=manager), doctype)
		# The Employee form reads HR Settings (D-248): read, never write.
		if frappe.db.exists("DocType", "HR Settings"):
			self.assertTrue(frappe.has_permission("HR Settings", "read", user=manager))
			self.assertFalse(frappe.has_permission("HR Settings", "write", user=manager))
			frappe.set_user(manager)
			frappe.get_doc("HR Settings").check_permission("read")
			frappe.set_user("Administrator")
		frappe.set_user(manager)
		doc = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Sidebar Worker",
				"gender": "Male",
				"date_of_birth": frappe.utils.add_years(frappe.utils.nowdate(), -22),
				"date_of_joining": frappe.utils.nowdate(),
				"company": fx.COMPANY,
				"status": "Active",
				"designation": "Cook",
				"cell_number": "01000000000",
			}
		).insert()
		doc.cell_number = "01111111111"
		doc.save()
		self.assertEqual(frappe.db.get_value("Employee", doc.name, "cell_number"), "01111111111")
		self.assertIn(doc.name, frappe.get_list("Employee", pluck="name"))


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



class TestAppIconLanding(IntegrationTestCase):
	"""The app icon opens /restaurant, which sends each user to a restaurant page they may open
	(D-240): the kitchen to its screen, every other Resto role to the floor."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.users = {role: user_with(role) for role in (*FLOOR_ROLES, "Resto Manager")}

	def tearDown(self):
		frappe.set_user("Administrator")

	def landing(self, user):
		from resto.www.restaurant import landing_route

		frappe.set_user(user)
		try:
			return landing_route()
		finally:
			frappe.set_user("Administrator")

	def test_the_icon_opens_the_landing_route(self):
		from resto import hooks

		self.assertEqual(hooks.add_to_apps_screen[0]["route"], "/restaurant")

	def test_each_role_lands_on_a_page_it_may_open(self):
		expected = {
			"Resto Kitchen": "/desk/resto-kitchen",
			"Resto Cashier": "/desk/resto-floor",
			"Resto Waiter": "/desk/resto-floor",
			"Resto Host": "/desk/resto-floor",
			"Resto Manager": "/desk/resto-floor",
		}
		for role, route in expected.items():
			with self.subTest(role=role):
				self.assertEqual(self.landing(self.users[role]), route)
				page = route.rsplit("/", 1)[1]
				frappe.set_user(self.users[role])
				self.assertTrue(frappe.get_doc("Page", page).is_permitted())
				frappe.set_user("Administrator")

	def test_a_guest_is_sent_to_log_in_first(self):
		from resto.www import restaurant

		frappe.set_user("Guest")
		with self.assertRaises(frappe.Redirect) as sent:
			restaurant.get_context(frappe._dict())
		self.assertEqual(sent.exception.http_status_code, 302)  # not kept by the browser
		self.assertTrue(frappe.local.flags.redirect_location.startswith("/login?redirect-to="))
		frappe.set_user(self.users["Resto Kitchen"])
		with self.assertRaises(frappe.Redirect):
			restaurant.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/desk/resto-kitchen")


class TestKitchenSeesNoMoney(IntegrationTestCase):
	"""The kitchen has nothing to do with bills or money (D-242): no order, no report, no price or
	cost on what it opens. It keeps its screen, tickets, items, recipe quantities, prep and waste drafts."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.kitchen = user_with("Resto Kitchen")
		cls.manager = user_with("Resto Manager")

	def tearDown(self):
		frappe.set_user("Administrator")

	def opened(self, user, doctype, name):
		"""The document as the form gets it (getdoc), for `user`."""
		from frappe.desk.form.load import getdoc

		frappe.set_user(user)
		try:
			frappe.local.response = frappe._dict({"docs": []})
			getdoc(doctype, name)
			return frappe.local.response.docs[0]
		finally:
			frappe.set_user("Administrator")

	def test_no_order_and_no_report(self):
		self.assertFalse(frappe.has_permission("Resto Order", "read", user=self.kitchen))
		self.assertTrue(frappe.has_permission("Resto Order", "select", user=self.kitchen))
		self.assertFalse(frappe.has_permission("Bin", "read", user=self.kitchen))
		shown = shown_links(self.kitchen)
		self.assertNotIn("Resto Order", shown)
		reports = frappe.get_all("Report", filters={"module": "Resto"}, pluck="name")
		self.assertTrue(reports)
		self.assertFalse(shown & set(reports))
		frappe.set_user(self.kitchen)
		for report in reports:
			with self.subTest(report=report):
				self.assertFalse(frappe.get_doc("Report", report).is_permitted())
		frappe.set_user("Administrator")
		self.assertTrue({"Resto Kitchen Ticket", "Item", "Resto Recipe", "Resto Prep", "Resto Waste"} <= shown)

	def test_the_kitchen_cannot_open_a_whole_order(self):
		from resto.resto import floor

		order = floor.create_order(order_type="Takeaway", customer_name="Kitchen Guest")
		frappe.set_user(self.kitchen)
		self.assertRaises(frappe.PermissionError, floor.get_order, order)

	def test_a_recipe_shows_its_quantities_not_its_cost(self):
		frappe.get_doc("Resto Recipe", "DISH-BURGER").update_cost_fields()
		kitchen = self.opened(self.kitchen, "Resto Recipe", "DISH-BURGER")
		manager = self.opened(self.manager, "Resto Recipe", "DISH-BURGER")
		# A value the user may not read is taken off the document: get() gives None.
		for field in ("total_cost", "selling_rate", "food_cost_percent", "main_cost", "cost_per_unit"):
			with self.subTest(field=field):
				self.assertIsNone(kitchen.get(field))
				self.assertIsNotNone(manager.get(field))
		self.assertTrue(kitchen.items and all(r.qty for r in kitchen.items))
		self.assertFalse([r for r in kitchen.items if r.get("rate") is not None or r.get("amount") is not None])
		self.assertGreater(frappe.utils.flt(manager.total_cost), 0)

	def test_an_item_shows_no_price_or_cost(self):
		for code in ("RAW-BEEF", "DISH-BURGER"):
			kitchen = self.opened(self.kitchen, "Item", code)
			manager = self.opened(self.manager, "Item", code)
			for field in ("valuation_rate", "standard_rate", "last_purchase_rate"):
				with self.subTest(item=code, field=field):
					self.assertIsNone(kitchen.get(field))
					self.assertIsNotNone(manager.get(field))
			self.assertEqual(kitchen.item_name, manager.item_name)
		self.assertGreater(frappe.utils.flt(self.opened(self.manager, "Item", "RAW-BEEF").valuation_rate), 0)
		# The recipe tab: its quantities, not its cost.
		burger = self.opened(self.kitchen, "Item", "DISH-BURGER")
		self.assertTrue(burger.resto_recipe_items and all(r.qty for r in burger.resto_recipe_items))
		self.assertFalse([r for r in burger.resto_recipe_items if r.get("rate") is not None])
		self.assertIsNone(burger.get("resto_recipe_total_cost"))
		self.assertIsNotNone(self.opened(self.manager, "Item", "DISH-BURGER").get("resto_recipe_total_cost"))


class TestManagerCountsStock(IntegrationTestCase):
	"""The manager posts the weekly stock count without Stock Manager (D-244); the kitchen cannot."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.manager = user_with("Resto Manager")
		cls.kitchen = user_with("Resto Kitchen")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_the_rights(self):
		for right in ("read", "write", "create", "submit", "cancel", "amend", "print"):
			with self.subTest(right=right):
				self.assertTrue(frappe.has_permission("Stock Reconciliation", right, user=self.manager))
				self.assertFalse(frappe.has_permission("Stock Reconciliation", right, user=self.kitchen))
		self.assertNotIn("Stock Manager", frappe.get_roles(self.manager))

	def test_the_manager_posts_a_count_to_the_stock_adjustment_account(self):
		before = fx.bin_qty("RAW-BUN", fx.MAIN)
		frappe.set_user(self.manager)
		doc = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": fx.COMPANY,
				"purpose": "Stock Reconciliation",
				"items": [{"item_code": "RAW-BUN", "warehouse": fx.MAIN, "qty": before + 1, "valuation_rate": 5}],
			}
		).insert()
		doc.submit()
		frappe.set_user("Administrator")
		self.assertAlmostEqual(fx.bin_qty("RAW-BUN", fx.MAIN) - before, 1, 6)
		adjustment = frappe.get_cached_value("Company", fx.COMPANY, "stock_adjustment_account")
		self.assertEqual(doc.expense_account, adjustment)
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Stock Reconciliation", "voucher_no": doc.name, "account": adjustment, "is_cancelled": 0},
			fields=["debit", "credit"],
		)
		self.assertTrue(gl)
		credit = sum(frappe.utils.flt(g.credit) - frappe.utils.flt(g.debit) for g in gl)
		# What the count changed in the stock's value, with the other side in Stock Adjustment.
		self.assertTrue(frappe.utils.flt(doc.difference_amount))
		self.assertAlmostEqual(frappe.utils.flt(credit), frappe.utils.flt(doc.difference_amount), 2)
		frappe.set_user(self.manager)
		frappe.get_doc("Stock Reconciliation", doc.name).cancel()
		frappe.set_user("Administrator")
		self.assertAlmostEqual(fx.bin_qty("RAW-BUN", fx.MAIN), before, 6)
