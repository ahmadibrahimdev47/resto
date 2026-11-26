"""The demo history generator (D-278) on three days: every order paid or cancelled, the kitchen's
tickets served, the shift opened and closed each day, the kitchen restocked, the clock and the
manager's PIN put back; a run carried on to a later end, and run again adding nothing.

bench --site test.resto run-tests --module resto.tests.test_demo
"""

import datetime

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, now_datetime, nowdate

from resto.resto import demo
from resto.tests import fixtures as fx
from resto.tests.test_drawer_cash import safe_account
from resto.tests.test_floor_customers_shift import _manager

FIELDS = ("main_safe_account", "require_pos_shift", "blind_shift_count")


class TestDemo(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		cls.safe = safe_account()
		if not frappe.get_all("Supplier", filters={"disabled": 0}, limit=1):
			fx.doc("Supplier", "Kitchen Supplier Test", supplier_name="Kitchen Supplier Test", supplier_group="All Supplier Groups")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in FIELDS}
		frappe.db.set_single_value("Resto Settings", {"main_safe_account": self.safe, "require_pos_shift": 0, "blind_shift_count": 0})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.db.sql("update `tabResto Order` set status = 'Cancelled' where status in ('Open', 'Bill Requested')")
		frappe.db.sql("delete from `tabDefaultValue` where parent = '__global' and defkey in (%s, %s)", (demo.MARK, demo.ARGS))
		frappe.clear_cache()
		frappe.local.conf.resto_demo_site = 1
		# The kitchen's raw stock taken out (rolled back after): the first day's restock must buy it.
		rows = [
			(code, qty)
			for code, qty in frappe.db.sql("select item_code, actual_qty from `tabBin` where warehouse = %s and actual_qty > 0", fx.KITCHEN)
			if frappe.db.get_value("Item", code, "item_group") != "Resto Prepared"
		]
		if rows:
			se = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Issue",
					"company": fx.COMPANY,
					"from_warehouse": fx.KITCHEN,
					"items": [{"item_code": c, "qty": q, "s_warehouse": fx.KITCHEN, "conversion_factor": 1} for c, q in rows],
				}
			)
			se.insert(ignore_permissions=True)
			se.submit()
		self.start = getdate(add_days(nowdate(), 3))
		self.end = getdate(add_days(self.start, 2))
		if not demo._fiscal_year(self.end, fx.COMPANY):
			self.skipTest("no fiscal year holds the test's days")

	def tearDown(self):
		frappe.local.conf.pop("resto_demo_site", None)
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", self._kept)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.clear_cache()

	def run_days(self, end=None, **kwargs):
		return demo.generate(start=str(self.start), end=str(end or self.end), orders_per_day=5, seed=3, opening=0, **kwargs)

	def orders(self):
		return frappe.get_all(
			"Resto Order",
			filters={"opened_at": ("between", (f"{self.start} 00:00:00", f"{self.end} 23:59:59")), "staff_meal": 0},
			fields=["name", "status", "order_type", "opened_at"],
		)

	def test_only_on_a_demo_site(self):
		frappe.local.conf.pop("resto_demo_site", None)
		self.assertRaises(frappe.ValidationError, self.run_days)
		self.assertRaises(frappe.ValidationError, demo.reset, confirm=frappe.local.site)

	def test_prices_that_start_later_are_found_and_moved(self):
		"""A site's prices are often valid from the day they were typed in: before it, no order has a price."""
		price = frappe.db.get_value("Item Price", {"item_code": "DISH-FRIES", "price_list": fx.PRICE_LIST, "selling": 1}, "name")
		frappe.db.set_value("Item Price", price, "valid_from", add_days(self.start, 5))
		self.assertGreaterEqual(demo._late_prices(self.start)["selling"], 1)
		self.assertRaises(frappe.ValidationError, self.run_days)
		self.assertGreaterEqual(demo._move_prices(self.start), 1)
		self.assertEqual(getdate(frappe.db.get_value("Item Price", price, "valid_from")), self.start)
		self.assertEqual(demo._late_prices(self.start)["selling"], 0)

	def test_the_same_seed_plans_the_same_days(self):
		site = demo._Site(3)
		one = demo._Planner(site, self.start, self.end, 5, 3).plan(self.start)
		two = demo._Planner(site, self.start, self.end, 5, 3).plan(self.start)
		key = lambda plan: [(s.at, s.kind, s.table, [(x.item_code, x.qty) for x in s.lines]) for s in plan.orders]  # noqa: E731
		self.assertEqual(key(one), key(two))
		self.assertTrue(one.orders)
		self.assertTrue(all(datetime.time(12) <= s.at.time() <= datetime.time(22, 41) for s in one.orders))

	def test_three_days(self):
		kept_pin = frappe.db.get_value("Resto Manager PIN", {"user": demo._Site(3).manager}, "pin_hash")
		# Two days first, then on to the third: the run goes on from the day after the last one done.
		second = getdate(add_days(self.start, 1))
		self.assertEqual(self.run_days(end=second), str(second))
		self.assertFalse([o for o in self.orders() if getdate(o.opened_at) == self.end])
		self.assertEqual(self.run_days(), str(self.end))
		# The clock is real again (the days played are ahead of today).
		self.assertLess(now_datetime().date(), self.start)
		self.assertEqual(frappe.db.get_value("Resto Manager PIN", {"user": demo._Site(3).manager}, "pin_hash"), kept_pin)
		self.assertEqual(frappe.db.get_global(demo.MARK), str(self.end))

		orders = self.orders()
		self.assertTrue(orders)
		self.assertEqual({o.status for o in orders} - {"Paid", "Cancelled"}, set())
		paid = [o for o in orders if o.status == "Paid"]
		self.assertGreaterEqual(len(paid), len(orders) * 0.7)
		for day in (self.start, add_days(self.start, 1), self.end):
			self.assertTrue(any(getdate(o.opened_at) == getdate(day) for o in paid), day)
			shifts = frappe.get_all(
				"Resto Shift",
				filters={"opening_time": ("between", (f"{day} 00:00:00", f"{day} 23:59:59"))},
				fields=["status", "closing_time"],
			)
			self.assertEqual([s.status for s in shifts], ["Closed"])
			self.assertEqual(getdate(shifts[0].closing_time), getdate(day))
		# Billed on its day, and the kitchen served it after it was ordered.
		for o in paid:
			invoices = frappe.get_all("Resto Order Invoice", filters={"parent": o.name}, pluck="sales_invoice")
			self.assertTrue(invoices)
			for si in invoices:
				self.assertEqual(getdate(frappe.db.get_value("Sales Invoice", si, "posting_date")), getdate(o.opened_at))
			for t in frappe.get_all(
				"Resto Kitchen Ticket", filters={"resto_order": o.name, "ticket_type": "Order"}, fields=["status", "ready_at"]
			):
				self.assertIn(t.status, ("Served", "Cancelled"))
				if t.ready_at:
					self.assertGreater(t.ready_at, o.opened_at)
		# Restocked on the first day, received into the kitchen.
		bought = frappe.get_all("Purchase Invoice", filters={"posting_date": self.start, "docstatus": 1, "update_stock": 1}, pluck="name")
		self.assertTrue(bought)
		self.assertTrue(frappe.db.exists("Purchase Invoice Item", {"parent": ("in", bought), "warehouse": fx.KITCHEN}))
		# The day's prep was made (the par level of the prepared items).
		self.assertTrue(frappe.db.exists("Resto Prep", {"posting_date": self.start, "docstatus": 1}))

		# Again with the same arguments: nothing more.
		count = len(orders)
		self.assertEqual(self.run_days(), str(self.end))
		self.assertEqual(len(self.orders()), count)
		# Other arguments are refused: the days done were planned with these.
		self.assertRaises(frappe.ValidationError, demo.generate, start=str(self.start), end=str(self.end), orders_per_day=9, seed=3, opening=0)
