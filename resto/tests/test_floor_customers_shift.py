"""Customers made and found at the counter, and the cashier's POSNext shift from the floor screen.

bench --site test.resto run-tests --module resto.tests.test_floor_customers_shift
"""

import json
import random

import frappe
from frappe.tests import IntegrationTestCase

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


class TestFloorShift(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()

	def setUp(self):
		if "pos_next" not in frappe.get_installed_apps():
			self.skipTest("POSNext is not installed")
		frappe.set_user("Administrator")
		if not frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		# A shift a test before left open.
		frappe.db.sql("update `tabPOS Opening Shift` set status = 'Closed' where user = %s and status = 'Open'", CASHIER)
		frappe.set_user(CASHIER)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_open_and_close_the_shift_from_the_floor(self):
		self.assertFalse(floor.get_floor()["shift"]["shift"])
		modes = shift.opening_data()["modes"]
		self.assertIn("Cash", modes)
		name = shift.open_shift(json.dumps({"Cash": 150}))
		self.assertEqual(billing.get_open_shift(CASHIER, fx.PROFILE), name)
		self.assertEqual(floor.get_floor()["shift"]["shift"], name)
		self.assertRaises(frappe.ValidationError, shift.open_shift, json.dumps({"Cash": 0}))

		data = shift.closing_data()
		cash = next(r for r in data["rows"] if r["mode_of_payment"] == "Cash")
		if not data["blind"]:
			self.assertEqual(cash["expected_amount"], 150)
		# Every payment method must be counted.
		if len(data["rows"]) > 1:
			self.assertRaises(frappe.ValidationError, shift.close_shift, json.dumps({"Cash": 150}))
		result = shift.close_shift(json.dumps({r["mode_of_payment"]: (140 if r["mode_of_payment"] == "Cash" else r["expected_amount"] or 0) for r in data["rows"]}))
		row = next(r for r in result["rows"] if r["mode_of_payment"] == "Cash")
		self.assertEqual((row["opening_amount"], row["expected_amount"], row["closing_amount"], row["difference"]), (150, 150, 140, -10))
		self.assertEqual(frappe.db.get_value("POS Closing Shift", result["closing"], "docstatus"), 1)
		self.assertFalse(billing.get_open_shift(CASHIER, fx.PROFILE))

	def test_a_negative_opening_is_refused(self):
		self.assertRaises(frappe.ValidationError, shift.open_shift, json.dumps({"Cash": -5}))


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
