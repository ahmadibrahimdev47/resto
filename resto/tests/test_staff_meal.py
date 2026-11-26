"""Staff meals (D-231, resto/resto/staff_meal.py): what the staff eat, taken from the kitchen's stock at
cost with no sale. Stock, the ledger and the shift must stay exact. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_staff_meal
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from resto.resto import approval, floor, kitchen, shift, staff_meal, units
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import CASHIER, MANAGER_USER, WAITER_USER, _manager
from resto.tests.test_sides import GROUP, RICE, setup_sides

WATCH = ("RAW-BUN", "SAUCE-GARLIC", "SAUCE-TAHINI", "SIDE-RICE", "RAW-POTATO", "RAW-CHEESE", "STAFF-TEA")
KITCHEN_USER = "kitchen@resto.test"


def bins():
	return {c: fx.bin_qty(c) for c in WATCH}


def moved(before):
	now = bins()
	return {c: round(before[c] - now[c], 6) for c in WATCH if abs(before[c] - now[c]) > 1e-9}


def sle_value(se):
	return flt(
		frappe.db.sql(
			"""select sum(stock_value_difference) from `tabStock Ledger Entry`
			where voucher_type = 'Stock Entry' and voucher_no = %s and is_cancelled = 0""",
			se,
		)[0][0]
	)


def gl(se):
	out = {}
	for r in frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Stock Entry", "voucher_no": se, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		out[r.account] = out.get(r.account, 0) + flt(r.debit) - flt(r.credit)
	return out


def stock_account():
	from erpnext.stock import get_warehouse_account_map

	return get_warehouse_account_map(fx.COMPANY)[fx.KITCHEN].account


def staff_account():
	expenses = frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
	return fx.account("Staff Meals", expenses, account_type="Expense Account")


class TestStaffMeal(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		setup_sides()
		_manager()
		# An item no recipe uses.
		fx.item("STAFF-TEA", "Kg", group="Resto Raw", valuation=80)
		fx.receive(fx.KITCHEN, [("RAW-CHEESE", 5, 200), ("RAW-POTATO", 10, 20), ("STAFF-TEA", 5, 80)])
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		fields = ("require_pos_shift", "blind_shift_count", "allow_side_changes", "charge_extra_sides", "staff_meal_account")
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in fields}
		frappe.db.set_single_value(
			"Resto Settings",
			{"require_pos_shift": 0, "blind_shift_count": 0, "allow_side_changes": 1, "charge_extra_sides": 0, "staff_meal_account": staff_account()},
		)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		if frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile"}) and not frappe.db.exists(
			"POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}
		):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.set_user(CASHIER)
		self.shift = shift.open_shift(json.dumps({"Cash": 500}))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", self._kept)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.flags.pop("resto_approvals", None)

	# ------------------------------------------------------------------ helpers

	def meal(self, *rows, staff_for="Kitchen team"):
		order = staff_meal.create(staff_for=staff_for)
		if rows:
			floor.add_items(order, json.dumps([{"item_code": c, "qty": q} for c, q in rows]))
		return order

	def line(self, order, item_code):
		return next(d for d in frappe.get_doc("Resto Order", order).items if d.item_code == item_code)

	def expected_cash(self):
		return next(r for r in shift.closing_data()["rows"] if r["mode_of_payment"] == "Cash")["expected_amount"]

	# ------------------------------------------------------------------ posting

	def test_posted_at_cost_into_the_staff_account_with_no_sale(self):
		order = self.meal(("DISH-PLATE", 2), ("DISH-FRIES", 1))
		# The plates without their rice; cheese for something not on the menu.
		floor.set_sides(order, json.dumps([self.line(order, "DISH-PLATE").name]), json.dumps({RICE: {}}))
		staff_meal.set_item(order, "RAW-CHEESE", 0.1)
		before = bins()
		result = staff_meal.post(order)

		doc = frappe.get_doc("Resto Order", order)
		self.assertEqual(doc.status, "Posted")
		self.assertEqual(doc.resto_shift, self.shift)
		se = frappe.get_doc("Stock Entry", doc.staff_stock_entry)
		self.assertEqual((se.docstatus, se.purpose), (1, "Material Issue"))
		self.assertEqual({d.s_warehouse for d in se.items}, {fx.KITCHEN})
		self.assertEqual({d.expense_account for d in se.items}, {staff_account()})
		self.assertEqual({d.cost_center for d in se.items}, {frappe.get_cached_value("Company", fx.COMPANY, "cost_center")})
		# Each dish as served: the plates' bun and garlic sauce but no rice, the fries' potato, the cheese.
		self.assertEqual(moved(before), {"RAW-BUN": 2, "SAUCE-GARLIC": 2, "RAW-POTATO": 0.25, "RAW-CHEESE": 0.1})
		# The ledger: the staff account takes exactly what the stock lost, at the stock's value.
		value = sle_value(se.name)
		self.assertLess(value, 0)
		g = gl(se.name)
		self.assertAlmostEqual(sum(g.values()), 0, 2)
		self.assertAlmostEqual(g.get(staff_account(), 0), -value, 2)
		self.assertAlmostEqual(g.get(stock_account(), 0), value, 2)
		self.assertAlmostEqual(flt(doc.staff_cost), -value, 2)
		self.assertAlmostEqual(result["cost"], -value, 2)
		# No sale of any kind.
		self.assertFalse(frappe.db.exists("Sales Invoice", {"resto_order": order}))
		self.assertFalse(doc.invoices)

	def test_the_kitchen_gets_it_marked_with_the_free_ingredients(self):
		order = self.meal(("DISH-FRIES", 1))
		staff_meal.set_item(order, "RAW-CHEESE", 0.05)
		staff_meal.post(order)
		doc = frappe.get_doc("Resto Order", order)
		self.assertEqual(self.line(order, "DISH-FRIES").kitchen_status, "Sent")
		tickets = frappe.get_all("Resto Kitchen Ticket", filters={"resto_order": order}, fields=["name", "order_type", "order_no"])
		self.assertTrue(tickets)
		self.assertEqual({t.order_type for t in tickets}, {"Staff Meal"})
		self.assertEqual({t.order_no for t in tickets}, {doc.order_no})
		# Shown in the tickets' list (D-273); a ticket from before it was kept gets it from its order.
		df = frappe.get_meta("Resto Kitchen Ticket").get_field("order_type")
		self.assertEqual((df.fieldtype, df.in_list_view, df.in_standard_filter), ("Select", 1, 1))
		from resto.patches.v0_1 import kitchen_ticket_order_type

		frappe.db.set_value("Resto Kitchen Ticket", tickets[0].name, "order_type", None)
		kitchen_ticket_order_type.execute()
		self.assertEqual(frappe.db.get_value("Resto Kitchen Ticket", tickets[0].name, "order_type"), "Staff Meal")
		rows = frappe.get_all(
			"Resto Kitchen Ticket Item", filters={"parent": ("in", [t.name for t in tickets])}, fields=["item_code", "qty", "order_item"]
		)
		free = [r for r in rows if r.item_code == "RAW-CHEESE"]
		self.assertEqual(len(free), 1)
		self.assertFalse(free[0].order_item)
		self.assertAlmostEqual(flt(free[0].qty), 0.05, 6)
		# Its number is the order's number of the day: no other order of the day has it.
		start = kitchen.business_day_start(doc.opened_at)
		day_orders = frappe.get_all("Resto Order", filters={"order_no": doc.order_no, "opened_at": (">=", start)}, pluck="name")
		self.assertEqual(day_orders, [order])

	def test_free_ingredients_only_needed(self):
		order = self.meal()
		staff_meal.set_item(order, "RAW-POTATO", 0.3)
		staff_meal.set_item(order, "RAW-POTATO", 0.2)  # changed, not added twice
		before = bins()
		staff_meal.post(order)
		self.assertEqual(moved(before), {"RAW-POTATO": 0.2})
		# A press again after a lost answer posts nothing more.
		self.assertEqual(staff_meal.post(order)["tickets"], [])
		self.assertEqual(moved(before), {"RAW-POTATO": 0.2})

	def test_nothing_to_post_and_no_account(self):
		order = self.meal()
		self.assertRaises(frappe.ValidationError, staff_meal.post, order)
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "staff_meal_account", None)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		self.assertRaises(frappe.ValidationError, staff_meal.create)

	# ------------------------------------------------------------------ free ingredients

	def test_an_unusual_quantity_needs_a_manager(self):
		order = self.meal()
		# 100 typed for 100 g of a Kg item: one dish uses at most 0.03 Kg of cheese.
		self.assertRaises(approval.ApprovalRequired, staff_meal.set_item, order, "RAW-CHEESE", 100)
		# An item no recipe uses, even a little.
		self.assertRaises(approval.ApprovalRequired, staff_meal.set_item, order, "STAFF-TEA", 0.05)
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user(CASHIER)
		staff_meal.set_item(order, "STAFF-TEA", 0.05, approver=MANAGER_USER, pin="4321")
		row = frappe.get_doc("Resto Order", order).staff_items[0]
		self.assertEqual((row.item_code, flt(row.qty), row.approved_by), ("STAFF-TEA", 0.05, MANAGER_USER))
		# Whole units stay whole (the fixtures leave Nos fractional: made whole here, as in test_sides);
		# a menu dish is added from the menu, not as an ingredient.
		frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 1)
		frappe.clear_document_cache("UOM", "Nos")
		self.addCleanup(frappe.clear_document_cache, "UOM", "Nos")
		self.addCleanup(frappe.db.set_value, "UOM", "Nos", "must_be_whole_number", 0)
		self.assertRaises(frappe.ValidationError, staff_meal.set_item, order, "RAW-BUN", 1.5)
		self.assertRaises(frappe.ValidationError, staff_meal.set_item, order, "DISH-FRIES", 1)
		# 0 takes it off.
		staff_meal.set_item(order, "STAFF-TEA", 0)
		self.assertFalse(frappe.get_doc("Resto Order", order).staff_items)

	# ------------------------------------------------------------------ no sale anywhere

	def test_no_bill_no_plain_send_and_out_of_sales(self):
		from resto.resto.report.resto_sales_analysis import resto_sales_analysis

		filters = {"company": fx.COMPANY, "from_date": nowdate(), "to_date": nowdate(), "group_by": "Day"}
		sales_before = resto_sales_analysis.execute(filters)[1]
		order = self.meal(("DISH-FRIES", 2))
		self.assertRaises(frappe.ValidationError, floor.get_bill, order)
		self.assertRaises(frappe.ValidationError, floor.send_to_kitchen, order)
		self.assertRaises(frappe.ValidationError, floor.request_bill, order)
		staff_meal.post(order)
		self.assertEqual(resto_sales_analysis.execute(filters)[1], sales_before)

	# ------------------------------------------------------------------ the shift

	def test_the_shift_shows_it_apart_and_not_as_cash(self):
		cash = self.expected_cash()
		order = self.meal(("DISH-FRIES", 1))
		staff_meal.post(order)
		cost = flt(frappe.db.get_value("Resto Order", order, "staff_cost"))
		self.assertGreater(cost, 0)
		self.assertAlmostEqual(self.expected_cash(), cash, 2)
		data = shift.closing_data()
		self.assertEqual(data["summary"]["staff_meals_count"], 1)
		self.assertAlmostEqual(data["summary"]["staff_meals_cost"], cost, 2)
		self.assertEqual(data["summary"]["sales_count"], 0)
		self.assertAlmostEqual(data["summary"]["net_cash"], 0, 2)
		self.assertEqual([m["order"] for m in data["staff_meals"]], [order])
		self.assertEqual(shift.shift_print_data(self.shift)["staff_meals"], {"count": 1, "cost": cost})

	# ------------------------------------------------------------------ cancel

	def test_a_manager_cancels_and_everything_comes_back(self):
		order = self.meal(("DISH-PLATE", 1))
		staff_meal.set_item(order, "RAW-CHEESE", 0.1)
		before = bins()
		staff_meal.post(order)
		se = frappe.db.get_value("Resto Order", order, "staff_stock_entry")
		# Not by the cashier, and the Stock Entry not on its own.
		self.assertRaises(frappe.PermissionError, staff_meal.cancel, order, "Wrong meal")
		frappe.set_user("Administrator")
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Stock Entry", se).cancel)
		frappe.set_user(MANAGER_USER)
		staff_meal.cancel(order, "Wrong meal")
		frappe.set_user(CASHIER)
		self.assertEqual(frappe.db.get_value("Stock Entry", se, "docstatus"), 2)
		self.assertEqual(moved(before), {})
		self.assertEqual(gl(se), {})
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Cancelled")
		self.assertEqual(shift.closing_data()["summary"]["staff_meals_count"], 0)
		open_tickets = frappe.get_all("Resto Kitchen Ticket", filters={"resto_order": order, "status": ("in", ("New", "In Progress"))})
		self.assertFalse(open_tickets)

	def test_before_posting_it_is_dropped_like_any_order(self):
		order = self.meal(("DISH-FRIES", 1))
		before = bins()
		floor.cancel_order(order, "Not needed")
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Cancelled")
		self.assertEqual(moved(before), {})
		self.assertFalse(frappe.db.get_value("Resto Order", order, "staff_stock_entry"))

	# ------------------------------------------------------------------ who

	def test_waiters_and_the_kitchen_cannot_make_one(self):
		order = self.meal(("DISH-FRIES", 1))
		for user in (WAITER_USER, KITCHEN_USER):
			frappe.set_user(user)
			with self.subTest(user=user):
				self.assertRaises(frappe.PermissionError, staff_meal.create)
				self.assertRaises(frappe.PermissionError, staff_meal.post, order)
				self.assertRaises(frappe.PermissionError, staff_meal.set_item, order, "RAW-POTATO", 0.1)
		frappe.set_user(WAITER_USER)
		# A waiter cannot add to it from the floor either.
		self.assertRaises(frappe.PermissionError, floor.add_items, order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		frappe.set_user(CASHIER)
		self.assertEqual(len(frappe.get_doc("Resto Order", order).items), 1)


def small_unit(case, code, stock_uom, small_uom):
	"""An item by the kilo or litre that converts to grams or millilitres (the site's own conversion,
	or an item row where the site has none)."""
	from erpnext.stock.get_item_details import get_conversion_factor

	for unit in (stock_uom, small_uom):
		if not frappe.db.exists("UOM", unit):
			fx.doc("UOM", unit, uom_name=unit)
	code = fx.item(code, stock_uom, group="Resto Raw", valuation=40).name
	if not flt((get_conversion_factor(code, small_uom) or {}).get("conversion_factor")):
		item = frappe.get_doc("Item", code)
		item.append("uoms", {"uom": small_uom, "conversion_factor": 0.001})
		item.save(ignore_permissions=True)
	case.assertEqual(flt((get_conversion_factor(code, small_uom) or {}).get("conversion_factor")), 0.001)
	return code


class TestTypedInRecipeUnits(IntegrationTestCase):
	"""A free ingredient and a waste row are typed in grams and millilitres, kept in the stock unit (D-250)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		self.cheese = small_unit(self, "RAW-CHEESE", "Kg", "Gram")
		self.milk = small_unit(self, "STAFF-MILK", "Litre", "Millilitre")
		fx.receive(fx.KITCHEN, [(self.milk, 5, 40)])
		fields = ("require_pos_shift", "blind_shift_count", "staff_meal_account")
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in fields}
		frappe.db.set_single_value("Resto Settings", {"require_pos_shift": 0, "blind_shift_count": 0, "staff_meal_account": staff_account()})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.set_user(CASHIER)
		shift.open_shift(json.dumps({"Cash": 500}))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", self._kept)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.flags.pop("resto_approvals", None)

	def line(self, order, item_code):
		return next(r for r in frappe.get_doc("Resto Order", order).staff_items if r.item_code == item_code)

	def test_the_dialog_offers_the_recipe_unit_first(self):
		got = units.item_units(self.cheese)
		self.assertEqual((got["stock_uom"], got["default"]), ("Kg", "Gram"))
		self.assertEqual({u["uom"]: u["factor"] for u in got["units"]}["Gram"], 0.001)
		self.assertEqual(got["units"][0]["uom"], "Gram")
		self.assertEqual(units.item_units("RAW-BUN")["default"], "Nos")

	def test_a_unit_the_item_does_not_convert_to_is_never_taken_as_one(self):
		# ERPNext answers 1 for an unknown unit; 5 Gram of buns must not become 5 buns.
		self.assertEqual(units.conversion("RAW-BUN", "Gram"), 0)
		self.assertRaises(frappe.ValidationError, units.to_stock, "RAW-BUN", 5, "Gram")
		self.assertEqual([u["uom"] for u in units.units_of("RAW-BUN")["units"]], ["Nos"])
		# The Unit picker of a waste row offers only what the item converts to.
		self.assertEqual(units.uom_query("UOM", "", "name", 0, 20, {"item_code": "RAW-BUN"}), [("Nos",)])
		offered = {r[0] for r in units.uom_query("UOM", "", "name", 0, 20, {"item_code": self.cheese})}
		self.assertTrue({"Gram", "Kg"} <= offered)
		self.assertNotIn("Nos", offered)

	def test_a_free_ingredient_in_grams_millilitres_or_whole_units(self):
		order = staff_meal.create(staff_for="Typed units")
		staff_meal.set_item(order, self.cheese, 20, uom="Gram")
		row = self.line(order, self.cheese)
		self.assertEqual((flt(row.qty, 6), row.uom), (0.02, "Kg"))
		# Judged on the stock quantity: 20 g of cheese is usual, 500 g (0.5 Kg, over ten dishes) is not.
		self.assertRaises(approval.ApprovalRequired, staff_meal.set_item, order, self.cheese, 500, uom="Gram")
		self.assertRaises(approval.ApprovalRequired, staff_meal.set_item, order, self.milk, 250, uom="Millilitre")
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user(CASHIER)
		staff_meal.set_item(order, self.milk, 250, MANAGER_USER, "4321", "Millilitre")
		row = self.line(order, self.milk)
		self.assertEqual((flt(row.qty, 6), row.uom), (0.25, "Litre"))
		staff_meal.set_item(order, "RAW-BUN", 2, uom="Nos")
		self.assertEqual(flt(self.line(order, "RAW-BUN").qty), 2)
		# No conversion: refused, nothing changed.
		self.assertRaises(frappe.ValidationError, staff_meal.set_item, order, "RAW-BUN", 20, uom="Gram")
		self.assertEqual(flt(self.line(order, "RAW-BUN").qty), 2)
		# The stock issue stays in the stock unit.
		result = staff_meal.post(order)
		issued = {d.item_code: (flt(d.qty, 6), d.uom) for d in frappe.get_doc("Stock Entry", result["stock_entry"]).items}
		self.assertEqual(issued[self.cheese], (0.02, "Kg"))
		self.assertEqual(issued[self.milk], (0.25, "Litre"))
		self.assertEqual(issued["RAW-BUN"], (2, "Nos"))

	def test_a_waste_row_in_grams(self):
		frappe.set_user("Administrator")
		doc = frappe.get_doc(
			{
				"doctype": "Resto Waste",
				"company": fx.COMPANY,
				"items": [
					{"item_code": self.cheese, "entered_qty": 20, "entered_uom": "Gram", "reason": "Spoiled"},
					{"item_code": self.milk, "entered_qty": 300, "entered_uom": "Millilitre", "reason": "Spoiled"},
					{"item_code": "RAW-BUN", "qty": 3, "reason": "Dropped"},
				],
			}
		).insert(ignore_permissions=True)
		rows = {r.item_code: r for r in doc.items}
		self.assertEqual((flt(rows[self.cheese].qty, 6), rows[self.cheese].uom), (0.02, "Kg"))
		self.assertEqual(flt(rows[self.milk].qty, 6), 0.3)
		# A row made in the stock unit shows it as typed.
		self.assertEqual((flt(rows["RAW-BUN"].entered_qty), rows["RAW-BUN"].entered_uom), (3, "Nos"))
		bad = frappe.get_doc(
			{"doctype": "Resto Waste", "company": fx.COMPANY, "items": [{"item_code": "RAW-BUN", "entered_qty": 5, "entered_uom": "Gram", "reason": "Dropped"}]}
		)
		self.assertRaises(frappe.ValidationError, bad.insert, ignore_permissions=True)
