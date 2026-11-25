"""Customers made and found at the counter, and the cashier's shift (Resto Shift) from the floor screen.

bench --site test.resto run-tests --module resto.tests.test_floor_customers_shift
"""

import json
import random

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from resto.resto import approval, billing, floor, shift
from resto.tests import fixtures as fx

CASHIER = "cashier@resto.test"


class TestFloorCustomers(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)

	def tearDown(self):
		frappe.set_user("Administrator")

	def phone(self, tail):
		# A phone no other test uses.
		return f"0109{tail}{random.randint(100000, 999999)}"

	def test_a_new_customer_from_the_delivery_dialog(self):
		phone = self.phone("1")
		order = floor.create_order(
			order_type="Delivery", customer_name="Mona Adel", phone=phone[:4] + " " + phone[4:], address="12 Nile St", new_customer=1
		)
		doc = frappe.get_doc("Resto Order", order)
		self.assertTrue(doc.customer)
		self.assertEqual(doc.phone, phone)
		c = frappe.db.get_value("Customer", doc.customer, ["customer_name", "mobile_no", "resto_address"], as_dict=True)
		self.assertEqual((c.customer_name, c.mobile_no, c.resto_address), ("Mona Adel", phone, "12 Nile St"))
		# ERPNext made its primary contact with the phone.
		self.assertTrue(
			frappe.db.sql(
				"""select 1 from `tabContact Phone` cp join `tabDynamic Link` dl on dl.parent = cp.parent
				where dl.link_doctype = 'Customer' and dl.link_name = %s and cp.phone = %s""",
				(doc.customer, phone),
			)
		)

	def test_the_same_phone_gives_the_same_customer(self):
		phone = self.phone("2")
		first = frappe.db.get_value("Resto Order", floor.create_order(order_type="Takeaway", customer_name="Ali", phone=phone, new_customer=1), "customer")
		second = frappe.db.get_value("Resto Order", floor.create_order(order_type="Takeaway", customer_name="Ali H", phone=phone, new_customer=1), "customer")
		self.assertEqual(first, second)

	def test_search_by_phone_name_or_address_and_picking_fills_in(self):
		phone = self.phone("3")
		order = floor.create_order(order_type="Delivery", customer_name="Karim Searchable", phone=phone, address="Tower 7, Maadi", new_customer=1)
		customer = frappe.db.get_value("Resto Order", order, "customer")
		for txt in (phone[-6:], "Searchable", "Tower 7"):
			self.assertIn(customer, [c.name for c in floor.search_customers(txt)], txt)
		# Picked on a new order: its phone and address come with it.
		again = frappe.get_doc("Resto Order", floor.create_order(order_type="Delivery", customer=customer))
		self.assertEqual((again.customer_name, again.phone, again.address), ("Karim Searchable", phone, "Tower 7, Maadi"))

	def test_a_waiter_cannot_create_a_customer(self):
		frappe.set_user("waiter@resto.test")
		self.assertRaises(
			frappe.PermissionError, floor.create_order, order_type="Takeaway", customer_name="Nobody", phone=self.phone("4"), new_customer=1
		)

	def test_order_info_links_a_new_customer(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Walk In")
		floor.update_order_info(order, customer_name="Hana Info", phone=self.phone("5"), new_customer=1)
		self.assertTrue(frappe.db.get_value("Resto Order", order, "customer"))
		floor.update_order_info(order, customer="")
		self.assertFalse(frappe.db.get_value("Resto Order", order, "customer"))

	def test_a_name_alone_saves_no_customer(self):
		before = frappe.db.count("Customer")
		order = floor.create_order(order_type="Takeaway", customer_name="Just A Name", new_customer=1)
		self.assertFalse(frappe.db.get_value("Resto Order", order, "customer"))
		self.assertEqual(frappe.db.get_value("Resto Order", order, "customer_name"), "Just A Name")
		self.assertEqual(frappe.db.count("Customer"), before)

	def test_another_customers_phone_is_refused_and_a_known_phone_links(self):
		a_phone, b_phone = self.phone("6"), self.phone("7")
		a = frappe.db.get_value("Resto Order", floor.create_order(order_type="Takeaway", customer_name="Owner A", phone=a_phone, new_customer=1), "customer")
		b = frappe.db.get_value("Resto Order", floor.create_order(order_type="Takeaway", customer_name="Owner B", phone=b_phone, new_customer=1), "customer")
		# B picked with A's phone: refused, nothing saved on either.
		self.assertRaises(frappe.ValidationError, floor.create_order, order_type="Takeaway", customer=b, phone=a_phone)
		self.assertEqual(frappe.db.get_value("Customer", b, "mobile_no"), b_phone)
		# A's phone typed, no one picked, no new customer asked: the order is A's.
		order = floor.create_order(order_type="Takeaway", phone=a_phone)
		self.assertEqual(frappe.db.get_value("Resto Order", order, "customer"), a)


class TestFloorShift(IntegrationTestCase):
	"""Resto's own shift (D-203): opened and closed on the floor, counting its own invoices."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.db.set_single_value("Resto Settings", "blind_shift_count", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		# A POS Profile that lists its users lets only them open a shift.
		if frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile"}) and not frappe.db.exists(
			"POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}
		):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		# A shift a test before left open.
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.set_user(CASHIER)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def takeaway(self, qty=1):
		order = floor.create_order(order_type="Takeaway")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": qty}]))
		return order, floor.get_bill(order)

	def test_open_bill_expense_and_close(self):
		self.assertFalse(floor.get_floor()["shift"]["shift"])
		self.assertIn("Cash", shift.opening_data()["modes"])
		name = shift.open_shift(json.dumps({"Cash": 150}))
		self.assertEqual(billing.get_open_shift(CASHIER, fx.PROFILE), name)
		self.assertEqual(floor.get_floor()["shift"]["shift"], name)
		# One shift per cashier.
		self.assertRaises(frappe.ValidationError, shift.open_shift, json.dumps({"Cash": 0}))

		# A bill paid in cash with change, one by card.
		order, bill = self.takeaway()
		paid = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"] + 50}]), None, bill["payable"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", paid["sales_invoice"], "resto_shift"), name)
		self.assertAlmostEqual(flt(paid["change_amount"]), 50, 2)
		order2, bill2 = self.takeaway(2)
		floor.settle(order2, json.dumps([{"mode_of_payment": "Card", "amount": bill2["payable"]}]), None, bill2["payable"])

		# Money paid out of the drawer: a Journal Entry from the cash account.
		account = frappe.db.sql(
			"""select name from `tabAccount` where company = %s and root_type = 'Expense' and is_group = 0 and disabled = 0
			and ifnull(account_type, '') in ('', 'Expense Account', 'Chargeable', 'Indirect Expense') limit 1""",
			fx.COMPANY,
		)[0][0]
		self.assertRaises(frappe.ValidationError, shift.add_expense, "Cash", account, 100000, "Too much")
		je = shift.add_expense("Cash", account, 20, "Ice")
		self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 1)
		cash_account = frappe.db.get_value("Mode of Payment Account", {"parent": "Cash", "company": fx.COMPANY}, "default_account")
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Journal Entry Account", {"parent": je, "account": cash_account}, "credit_in_account_currency")), 20, 2
		)

		data = shift.closing_data()
		rows = {r["mode_of_payment"]: r for r in data["rows"]}
		self.assertAlmostEqual(rows["Cash"]["expected_amount"], 150 + bill["payable"] - 20, 2)
		self.assertAlmostEqual(rows["Card"]["expected_amount"], bill2["payable"], 2)
		self.assertEqual(data["summary"]["sales_count"], 2)
		self.assertAlmostEqual(data["summary"]["gross_sales"], bill["payable"] + bill2["payable"], 2)
		self.assertAlmostEqual(data["summary"]["expenses_total"], 20, 2)
		# Every payment method must be counted.
		self.assertRaises(frappe.ValidationError, shift.close_shift, json.dumps({"Cash": 150}))
		counted = {"Cash": rows["Cash"]["expected_amount"] - 10, "Card": rows["Card"]["expected_amount"]}
		result = shift.close_shift(json.dumps(counted))
		doc = frappe.get_doc("Resto Shift", result["closing"])
		self.assertEqual(doc.status, "Closed")
		cash = next(r for r in doc.payments if r.mode_of_payment == "Cash")
		self.assertAlmostEqual(flt(cash.difference), -10, 2)
		self.assertAlmostEqual(flt(doc.total_difference), -10, 2)
		self.assertEqual(len(doc.invoices), 2)
		self.assertFalse(billing.get_open_shift(CASHIER, fx.PROFILE))
		# Closed once: a second close (a double tap) finds no open shift; it cannot be edited by hand.
		self.assertRaises(frappe.ValidationError, shift.close_shift, json.dumps(counted))
		frappe.set_user("Administrator")
		doc.remarks = "x"
		self.assertRaises(frappe.ValidationError, doc.save)
		# What a closed shift counted cannot be cancelled: a bill, a drawer expense.
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Sales Invoice", paid["sales_invoice"]).cancel)
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", je).cancel)

	def test_no_order_without_an_open_shift_when_required(self):
		frappe.set_user("Administrator")
		order = floor.create_order(order_type="Takeaway")  # made while shifts were not required
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		dish = json.dumps([{"item_code": "DISH-FRIES", "qty": 1}])
		# No shift open anywhere: neither the cashier nor a waiter can start or add to an order (D-211).
		frappe.set_user(CASHIER)
		self.assertFalse(floor.get_floor()["can_order"])
		self.assertRaises(frappe.ValidationError, floor.create_order, order_type="Takeaway")
		self.assertRaises(frappe.ValidationError, floor.add_items, order, dish)
		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.ValidationError, floor.create_order, order_type="Takeaway")

		# The cashier opens their shift: both can work, and the bill is on that shift.
		frappe.set_user(CASHIER)
		name = shift.open_shift(json.dumps({"Cash": 0}))
		frappe.set_user("waiter@resto.test")
		self.assertTrue(floor.get_floor()["can_order"])
		floor.add_items(order, dish)
		frappe.set_user(CASHIER)
		bill = floor.get_bill(order)
		r = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]), None, bill["payable"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", r["sales_invoice"], "resto_shift"), name)
		data = shift.closing_data()
		shift.close_shift(json.dumps({x["mode_of_payment"]: x["expected_amount"] for x in data["rows"]}))
		# Closed: nothing new until a shift is open again.
		self.assertRaises(frappe.ValidationError, floor.create_order, order_type="Takeaway")
		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.ValidationError, floor.create_order, order_type="Takeaway")
		# Nor from the order form or the REST API.
		form = frappe.get_doc({"doctype": "Resto Order", "order_type": "Takeaway", "items": [{"item_code": "DISH-FRIES", "qty": 1}]})
		self.assertRaises(frappe.ValidationError, form.insert)

	def test_taps_come_together_and_the_order_comes_back(self):
		order = floor.create_order(order_type="Takeaway")
		view = floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 3}, {"item_code": "DRK-COLA", "qty": 1}]), 1)
		self.assertEqual(view["name"], order)
		self.assertEqual(sorted((d["item_code"], d["qty"]) for d in view["items"]), [("DISH-FRIES", 3), ("DRK-COLA", 1)])
		self.assertAlmostEqual(flt(view["net_total"]), 3 * 40 + 25, 2)
		line = next(d["name"] for d in view["items"] if d["item_code"] == "DRK-COLA")
		view = floor.update_line(order, line, 2, view=1)
		self.assertEqual(next(d["qty"] for d in view["items"] if d["name"] == line), 2)
		view = floor.remove_line(order, line, 1)
		self.assertEqual([d["item_code"] for d in view["items"]], ["DISH-FRIES"])

	def test_a_change_sent_again_after_the_connection_dropped_counts_once(self):
		order = floor.create_order(order_type="Takeaway")
		items = json.dumps([{"item_code": "DISH-FRIES", "qty": 2}])
		floor.add_items(order, items, 1, "op-a1")
		view = floor.add_items(order, items, 1, "op-a1")  # the answer was lost; the screen sends it again
		self.assertEqual([(d["item_code"], d["qty"]) for d in view["items"]], [("DISH-FRIES", 2)])
		line = view["items"][0]["name"]
		# + and − send a step: what another screen added meanwhile stays.
		floor.update_line(order, line, step=1, view=1, op="op-s1")
		self.assertEqual(floor.update_line(order, line, step=1, view=1, op="op-s1")["items"][0]["qty"], 3)
		self.assertEqual(floor.update_line(order, line, step=-1, view=1, op="op-s2")["items"][0]["qty"], 2)
		floor.remove_line(order, line, 1, "op-r1")
		self.assertEqual(floor.remove_line(order, line, 1, "op-r1")["items"], [])
		floor.add_items(order, items, 1, "op-a2")  # a new change is applied
		self.assertEqual(len(floor.get_order(order)["items"]), 1)
		self.assertRaises(frappe.ValidationError, floor.add_items, order, items, 1, "bad id; drop")

	def test_a_debt_paid_to_the_cashier_is_in_the_drawer(self):
		# A bill left partly on account (by the manager, who gives credit to any named customer).
		frappe.set_user("Administrator")
		frappe.db.set_value("POS Profile", fx.PROFILE, "allow_partial_payment", 1)
		frappe.clear_document_cache("POS Profile", fx.PROFILE)
		try:
			order = floor.create_order(
				order_type="Takeaway", customer_name="Debtor", phone=f"0107{random.randint(1000000, 9999999)}", new_customer=1
			)
			floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
			bill = floor.get_bill(order)
			paid = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": 10}]), None, bill["payable"], None, 1)
		finally:
			frappe.db.set_value("POS Profile", fx.PROFILE, "allow_partial_payment", 0)
			frappe.clear_document_cache("POS Profile", fx.PROFILE)
		customer = frappe.db.get_value("Resto Order", order, "customer")
		owed = flt(bill["payable"]) - 10

		# The cashier collects part of it on the floor, in their shift.
		frappe.set_user(CASHIER)
		name = shift.open_shift(json.dumps({"Cash": 0}))
		self.assertAlmostEqual(shift.customer_debt(customer)["total"], owed, 2)
		self.assertRaises(frappe.ValidationError, shift.collect_debt, customer, owed + 100, "Cash")
		pe = shift.collect_debt(customer, 5, "Cash", "req-debt")
		self.assertEqual(shift.collect_debt(customer, 5, "Cash", "req-debt"), pe)  # a retried click
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, ["resto_shift", "docstatus"]), (name, 1))
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", paid["sales_invoice"], "outstanding_amount")), owed - 5, 2)
		data = shift.closing_data()
		self.assertAlmostEqual(next(r for r in data["rows"] if r["mode_of_payment"] == "Cash")["expected_amount"], 5, 2)
		self.assertAlmostEqual(data["summary"]["debts_total"], 5, 2)

	def test_a_negative_opening_is_refused(self):
		self.assertRaises(frappe.ValidationError, shift.open_shift, json.dumps({"Cash": -5}))

	def test_blind_count_hides_what_is_expected(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "blind_shift_count", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		shift.open_shift(json.dumps({"Cash": 10}))
		data = shift.closing_data()
		self.assertTrue(data["blind"])
		self.assertNotIn("expected_amount", data["rows"][0])
		result = shift.close_shift(json.dumps({r["mode_of_payment"]: 0 for r in data["rows"]}))
		self.assertAlmostEqual(next(r for r in result["rows"] if r["mode_of_payment"] == "Cash")["difference"], -10, 2)

	def test_invoice_history_live_figures_and_shift_history(self):
		from resto.resto import history

		name = shift.open_shift(json.dumps({"Cash": 0}))
		order, bill = self.takeaway(2)
		paid = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]), None, bill["payable"])
		si = paid["sales_invoice"]

		found = history.invoices(txt=si)["invoices"]
		self.assertEqual([r.name for r in found], [si])
		self.assertEqual(found[0].modes, ["Cash"])
		self.assertAlmostEqual(found[0].total, flt(bill["payable"]), 2)
		self.assertIn(si, [r.name for r in history.invoices(status="paid")["invoices"]])
		self.assertNotIn(si, [r.name for r in history.invoices(status="unpaid")["invoices"]])
		self.assertNotIn(si, [r.name for r in history.invoices(period="yesterday")["invoices"]])
		self.assertIn(si, [r.name for r in history.invoices(txt=si, period="any")["invoices"]])

		live = history.live()
		self.assertEqual(live["shift"], name)
		self.assertGreaterEqual(live["sales_count"], 1)
		self.assertAlmostEqual(live["net_cash"], flt(bill["payable"]), 2)
		# Counted blind: the running money is not shown to the cashier.
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "blind_shift_count", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		self.assertNotIn("net_cash", history.live())

		rows = history.shifts()
		self.assertEqual(rows[0].name, name)
		self.assertIsNone(rows[0].net_sales)  # open: its figures are the live ones
		self.assertTrue(all(r.user == CASHIER for r in rows))
		# A waiter has no history to see.
		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.PermissionError, history.invoices)


class TestSameDishTappedAgain(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def lines(self, order):
		return [(d.item_code, d.qty, d.kitchen_status) for d in frappe.get_doc("Resto Order", order).items]

	def test_a_second_tap_raises_the_quantity(self):
		order = floor.create_order(order_type="Takeaway", customer_name="Tap Guest")
		tap = json.dumps([{"item_code": "DISH-FRIES", "qty": 1}])
		floor.add_items(order, tap)
		floor.add_items(order, tap)
		self.assertEqual(self.lines(order), [("DISH-FRIES", 2, "Not Sent")])
		# A line with a note of its own stays apart, and so does anything after sending.
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1, "notes": "no salt"}]))
		floor.send_to_kitchen(order)
		floor.add_items(order, tap)
		self.assertEqual(
			self.lines(order), [("DISH-FRIES", 2, "Sent"), ("DISH-FRIES", 1, "Sent"), ("DISH-FRIES", 1, "Not Sent")]
		)


class TestPOSNextFixturePermissions(IntegrationTestCase):
	def test_a_doctype_posnext_touched_keeps_its_standard_rules(self):
		from resto.setup.install import FIXTURE_ROLES, repair_fixture_permissions

		repair_fixture_permissions()
		touched = set(frappe.get_all("Custom DocPerm", filters={"role": ("in", FIXTURE_ROLES)}, pluck="parent"))
		if not touched:
			self.skipTest("No POSNext permission fixtures on this site")
		for doctype in touched:
			standard = {r for r in frappe.get_all("DocPerm", filters={"parent": doctype, "parenttype": "DocType"}, pluck="role")}
			custom = set(frappe.get_all("Custom DocPerm", filters={"parent": doctype}, pluck="role"))
			if standard - set(FIXTURE_ROLES):
				self.assertTrue(custom & standard, doctype)


MANAGER_USER = "manager@resto.test"
WAITER_USER = "waiter@resto.test"


def _manager():
	if not frappe.db.exists("User", MANAGER_USER):
		u = frappe.get_doc({"doctype": "User", "email": MANAGER_USER, "first_name": "Resto Manager", "send_welcome_email": 0})
		u.insert(ignore_permissions=True)
		u.add_roles("Resto Manager")


class TestManagerApproval(IntegrationTestCase):
	"""A manager's PIN on a waiter's or cashier's screen (D-204): for that action and amount, logged."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()

	def setUp(self):
		frappe.flags.pop("resto_approvals", None)
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)

	def sent_order(self, qty=1):
		order = floor.create_order(order_type="Takeaway", customer_name="PIN Guest")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": qty}]))
		floor.send_to_kitchen(order)
		return order, frappe.get_doc("Resto Order", order).items[0].name

	def tries(self):
		return int(frappe.cache.get(approval._tries_key(MANAGER_USER)) or 0)

	def logs(self, **filters):
		return frappe.get_all("Resto Approval Log", filters=filters, fields=["approver", "requested_by", "result", "amount"], order_by="creation")

	def test_a_void_after_sending_needs_a_managers_pin(self):
		order, line = self.sent_order()
		frappe.set_user(WAITER_USER)
		self.assertRaises(approval.ApprovalRequired, floor.void_line, order, line, "Wrong dish")
		self.assertRaises(approval.ApprovalRequired, floor.void_line, order, line, "Wrong dish", 0, MANAGER_USER, "1111")
		self.assertEqual(self.tries(), 1)
		# Only a manager approves, never the one asking.
		self.assertRaises(approval.ApprovalRequired, floor.void_line, order, line, "Wrong dish", 0, "cashier@resto.test", "4321")
		floor.void_line(order, line, "Wrong dish", 0, MANAGER_USER, "٤٣٢١")  # typed on an Arabic keypad
		self.assertEqual(frappe.db.get_value("Resto Order Item", line, "kitchen_status"), "Void")
		self.assertEqual(self.tries(), 0)
		frappe.set_user("Administrator")
		logs = self.logs(reference=("like", f"{order}%"))
		self.assertEqual([lg.result for lg in logs], ["Refused", "Approved"])  # the cashier was never a try
		self.assertEqual((logs[-1].approver, logs[-1].requested_by), (MANAGER_USER, WAITER_USER))
		self.assertAlmostEqual(flt(logs[-1].amount), 40, 2)

	def test_five_wrong_pins_lock_it(self):
		order, line = self.sent_order()
		frappe.set_user(WAITER_USER)
		for _i in range(approval.MAX_TRIES):
			self.assertRaises(approval.ApprovalRequired, floor.cancel_order, order, "Guest left", None, MANAGER_USER, "0000")
		self.assertTrue(approval.locked_until(MANAGER_USER))
		# Locked: even the right PIN waits.
		self.assertRaises(approval.ApprovalRequired, floor.cancel_order, order, "Guest left", None, MANAGER_USER, "4321")
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Open")
		# A new PIN set by the manager unlocks it.
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("5678")
		frappe.set_user(WAITER_USER)
		floor.cancel_order(order, "Guest left", None, MANAGER_USER, "5678")
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Cancelled")

	def test_a_discount_above_the_cashiers_limit(self):
		order = floor.create_order(order_type="Takeaway", customer_name="PIN Discount")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 10}]))  # 400
		frappe.set_user("cashier@resto.test")
		self.assertEqual(floor.get_floor()["discount_limit"], 10)
		self.assertRaises(frappe.PermissionError, floor.apply_discount, order, 15, 0, "regular")
		self.assertRaises(approval.ApprovalRequired, floor.apply_discount, order, 15, 0, "regular", MANAGER_USER, "9999")
		floor.apply_discount(order, 15, 0, "regular", MANAGER_USER, "4321")
		# Approved as 60, not as 15%: more items later do not grow it without a new approval.
		self.assertEqual(
			[flt(v) for v in frappe.db.get_value("Resto Order", order, ["discount_amount", "discount_percentage"])], [60, 0]
		)
		frappe.set_user("Administrator")
		self.assertAlmostEqual(flt(self.logs(reference=("like", f"{order}%"), result="Approved")[-1].amount), 60, 2)

	def test_a_bill_on_account_without_a_credit_limit(self):
		frappe.db.set_value("POS Profile", fx.PROFILE, "allow_partial_payment", 1)
		frappe.clear_document_cache("POS Profile", fx.PROFILE)
		try:
			order = floor.create_order(
				order_type="Takeaway", customer_name="PIN Debtor", phone=f"0106{random.randint(1000000, 9999999)}", new_customer=1
			)
			floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
			frappe.set_user("cashier@resto.test")
			bill = floor.get_bill(order)
			self.assertTrue(bill["on_account_needs_approval"])
			pay = json.dumps([{"mode_of_payment": "Cash", "amount": 10}])
			self.assertRaises(approval.ApprovalRequired, floor.settle, order, pay, None, bill["payable"], None, 1)
			r = floor.settle(order, pay, None, bill["payable"], "req-pin", 1, MANAGER_USER, "4321")
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("POS Profile", fx.PROFILE, "allow_partial_payment", 0)
			frappe.clear_document_cache("POS Profile", fx.PROFILE)
		owed = flt(bill["payable"]) - 10
		self.assertAlmostEqual(flt(r["outstanding_amount"]), owed, 2)
		logs = self.logs(reference=("like", f"{order}%"), result="Approved")
		self.assertEqual(len(logs), 1)  # checked twice in the call, approved and logged once
		self.assertAlmostEqual(flt(logs[0].amount), owed, 2)

	def test_a_long_reference_is_still_logged(self):
		frappe.set_user(WAITER_USER)
		self.assertRaises(approval.ApprovalRequired, approval.approve, "void", "X" * 300, 1, MANAGER_USER, "0000")
		self.assertEqual(self.tries(), 1)
		frappe.set_user("Administrator")
		self.assertTrue(self.logs(reference="X" * 140, result="Refused"))

	def test_the_log_cannot_be_changed(self):
		order, line = self.sent_order()
		floor.void_line(order, line, "Manager voids")
		log = frappe.get_doc("Resto Approval Log", {"reference": ("like", f"{order}%")})
		self.assertEqual(log.approver, "Administrator")
		log.result = "Refused"
		self.assertRaises(frappe.ValidationError, log.save)
		self.assertRaises(frappe.ValidationError, log.delete)


class TestDrawerControls(IntegrationTestCase):
	"""Drawer expenses: allowed accounts, a cap per shift, a receipt (D-205); cash over/short (D-206)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		group = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
		cls.ice = fx.account("Drawer Ice", group, account_type="Expense Account")
		cls.other = fx.account("Drawer Other", group, account_type="Expense Account")
		cls.over_short = fx.account("Cash Over Short", group, account_type="Expense Account")

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		for field, value in (("require_pos_shift", 0), ("blind_shift_count", 0)):
			frappe.db.set_single_value("Resto Settings", field, value)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		if frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile"}) and not frappe.db.exists(
			"POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}
		):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		frappe.set_user(CASHIER)
		self.shift = shift.open_shift(json.dumps({"Cash": 500}))

	def tearDown(self):
		frappe.set_user("Administrator")
		for field, value in (("drawer_expense_cap", 0), ("drawer_expense_receipt", 0), ("cash_over_short_account", None)):
			frappe.db.set_single_value("Resto Settings", field, value)
		frappe.db.delete("Resto Expense Account", {"parent": "Resto Settings"})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def settings(self, **values):
		frappe.set_user("Administrator")
		s = frappe.get_single("Resto Settings")
		for k, v in values.items():
			s.set(k, v)
		s.save(ignore_permissions=True)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)

	def test_only_the_allowed_accounts_up_to_the_cap(self):
		self.settings(drawer_expense_accounts=[{"account": self.ice}], drawer_expense_cap=30)
		data = shift.expense_accounts()
		self.assertEqual([a.name for a in data["accounts"]], [self.ice])
		self.assertAlmostEqual(data["cap_left"], 30, 2)
		self.assertRaises(frappe.ValidationError, shift.add_expense, "Cash", self.other, 5, "Not allowed")
		shift.add_expense("Cash", self.ice, 20, "Ice")
		self.assertRaises(frappe.ValidationError, shift.add_expense, "Cash", self.ice, 15, "Over the cap")
		shift.add_expense("Cash", self.ice, 10, "Ice again")
		self.assertAlmostEqual(shift.expense_accounts()["cap_left"], 0, 2)

	def test_the_receipt_goes_on_the_entry(self):
		self.settings(drawer_expense_receipt=1)
		self.assertRaises(frappe.ValidationError, shift.add_expense, "Cash", self.ice, 5, "No receipt")
		f = frappe.get_doc({"doctype": "File", "file_name": f"receipt-{random.randint(1000, 9999)}.txt", "content": "receipt", "is_private": 1})
		f.flags.ignore_permissions = True
		f.insert()
		je = shift.add_expense("Cash", self.ice, 5, "Ice", None, f.file_url)
		self.assertEqual(frappe.db.get_value("File", f.name, ["attached_to_doctype", "attached_to_name"]), ("Journal Entry", je))
		# Used once: the same file is not a second receipt.
		self.assertRaises(frappe.ValidationError, shift.add_expense, "Cash", self.ice, 5, "Ice", None, f.file_url)

	def test_a_short_drawer_is_booked_and_printed(self):
		self.settings(cash_over_short_account=self.over_short)
		data = shift.closing_data()
		counted = {r["mode_of_payment"]: r["expected_amount"] for r in data["rows"]}
		counted["Cash"] -= 10
		result = shift.close_shift(json.dumps(counted))
		doc = frappe.get_doc("Resto Shift", result["closing"])
		self.assertTrue(doc.over_short_entry)
		rows = frappe.get_all(
			"Journal Entry Account", filters={"parent": doc.over_short_entry}, fields=["account", "debit_in_account_currency", "credit_in_account_currency"]
		)
		cash = frappe.db.get_value("Mode of Payment Account", {"parent": "Cash", "company": fx.COMPANY}, "default_account")
		by = {r.account: r for r in rows}
		self.assertAlmostEqual(flt(by[self.over_short].debit_in_account_currency), 10, 2)
		self.assertAlmostEqual(flt(by[cash].credit_in_account_currency), 10, 2)
		self.assertEqual(len(rows), 2)  # the card counted right: no line for it
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", doc.over_short_entry).cancel)
		html = frappe.get_print("Resto Shift", doc.name, "Resto Shift Close")
		self.assertIn(doc.name, html)
