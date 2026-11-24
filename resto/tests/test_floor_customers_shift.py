"""Customers made and found at the counter, and the cashier's shift (Resto Shift) from the floor screen.

bench --site test.resto run-tests --module resto.tests.test_floor_customers_shift
"""

import json
import random

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from resto.resto import billing, floor, shift
from resto.tests import fixtures as fx

CASHIER = "cashier@resto.test"


class TestFloorCustomers(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
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

	def test_a_bill_needs_an_open_shift_when_required(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		order, bill = self.takeaway()
		pay = json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}])
		self.assertRaises(frappe.ValidationError, floor.settle, order, pay, None, bill["payable"])
		name = shift.open_shift(json.dumps({"Cash": 0}))
		r = floor.settle(order, pay, None, bill["payable"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", r["sales_invoice"], "resto_shift"), name)
		data = shift.closing_data()
		shift.close_shift(json.dumps({x["mode_of_payment"]: x["expected_amount"] for x in data["rows"]}))
		order2, bill2 = self.takeaway()
		self.assertRaises(
			frappe.ValidationError, floor.settle, order2, json.dumps([{"mode_of_payment": "Cash", "amount": bill2["payable"]}]), None, bill2["payable"]
		)

	def test_a_debt_paid_to_the_cashier_is_in_the_drawer(self):
		name = shift.open_shift(json.dumps({"Cash": 0}))
		customer = frappe.db.get_value("Resto Order", floor.create_order(order_type="Takeaway", customer_name="Debtor", phone=f"0107{random.randint(1000000, 9999999)}", new_customer=1), "customer")
		cash = frappe.db.get_value("Mode of Payment Account", {"parent": "Cash", "company": fx.COMPANY}, "default_account")
		from erpnext.accounts.party import get_party_account

		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"company": fx.COMPANY,
				"party_type": "Customer",
				"party": customer,
				"mode_of_payment": "Cash",
				"paid_from": get_party_account("Customer", customer, fx.COMPANY),
				"paid_to": cash,
				"paid_amount": 35,
				"received_amount": 35,
				"reference_no": "debt",
				"reference_date": frappe.utils.nowdate(),
			}
		)
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
		self.assertEqual(pe.resto_shift, name)
		data = shift.closing_data()
		self.assertAlmostEqual(next(r for r in data["rows"] if r["mode_of_payment"] == "Cash")["expected_amount"], 35, 2)
		self.assertAlmostEqual(data["summary"]["debts_total"], 35, 2)

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


class TestSameDishTappedAgain(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

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
