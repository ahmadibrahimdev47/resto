"""Sides served with a dish («يُقدَّم مع»), changed on the floor screen (resto/resto/sides.py).

The release gate of "Let Staff Change the Sides Served": stock, cost of goods sold, cost per dish,
cancel and full and part returns must stay exact. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_sides
"""

import json

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from resto.resto import floor, sides
from resto.resto.report_utils import cost_by_invoice_row
from resto.tests import fixtures as fx

GROUP = "Test Sauces"
RICE = "item:SIDE-RICE"
WATCH = ("RAW-BUN", "RAW-BEEF", "SIDE-RICE", "SAUCE-GARLIC", "SAUCE-TAHINI")


def setup_sides():
	for code, uom, rate in (("SIDE-RICE", "Kg", 30), ("SAUCE-GARLIC", "Nos", 3), ("SAUCE-TAHINI", "Nos", 4)):
		fx.item(code, uom, group="Resto Raw", valuation=rate)
	for code, rate in (("DISH-PLATE", 120), ("DISH-GRILL", 160)):
		fx.item(code, stock=0, group="Resto Food")
		fx.price(code, rate)
	fx.doc(
		"Resto Side Group",
		GROUP,
		group_name=GROUP,
		max_portions=2,
		options=[{"item": "SAUCE-GARLIC", "portion_qty": 1}, {"item": "SAUCE-TAHINI", "portion_qty": 1}],
	)
	dish("DISH-PLATE", [("RAW-BUN", 1)], [("SIDE-RICE", 0.2, None), ("SAUCE-GARLIC", 1, GROUP)])
	dish("DISH-GRILL", [("RAW-BEEF", 0.2)], [("SAUCE-GARLIC", 1, GROUP)])
	fx.receive(
		fx.KITCHEN,
		[("SIDE-RICE", 20, 30), ("SAUCE-GARLIC", 200, 3), ("SAUCE-TAHINI", 200, 4), ("RAW-BUN", 50, 5), ("RAW-BEEF", 10, 300)],
	)


def dish(code, mains, served_with):
	if frappe.db.exists("Resto Recipe", code):
		return frappe.get_doc("Resto Recipe", code)
	uom = lambda c: frappe.get_cached_value("Item", c, "stock_uom")  # noqa: E731
	r = frappe.get_doc(
		{
			"doctype": "Resto Recipe",
			"recipe_type": "Menu Dish",
			"item": code,
			"company": fx.COMPANY,
			"quantity": 1,
			"items": [{"item_code": c, "qty": q, "uom": uom(c)} for c, q in mains],
			"sides": [{"item_code": c, "qty": q, "uom": uom(c), "side_group": g} for c, q, g in served_with],
		}
	)
	r.insert(ignore_permissions=True)
	return r


def lines(order):
	return frappe.get_doc("Resto Order", order).items


def bins():
	return {c: fx.bin_qty(c) for c in WATCH}


def moved(before):
	"""Stock taken out since `before` (positive: deducted)."""
	now = bins()
	return {c: round(before[c] - now[c], 6) for c in WATCH if abs(before[c] - now[c]) > 1e-9}


def packed(si, row):
	out = {}
	for p in si.packed_items:
		if p.parent_detail_docname == row.name:
			out[p.item_code] = round(out.get(p.item_code, 0) + flt(p.qty), 6)
	return out


def sle_value(voucher_no):
	return flt(
		frappe.db.sql(
			"""select sum(stock_value_difference) from `tabStock Ledger Entry`
			where voucher_type='Sales Invoice' and voucher_no=%s and is_cancelled=0""",
			voucher_no,
		)[0][0]
	)


def gl_by_account(voucher_no):
	out = {}
	for r in frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Sales Invoice", "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		out[r.account] = out.get(r.account, 0) + flt(r.debit) - flt(r.credit)
	return out


def stock_account():
	from erpnext.stock import get_warehouse_account_map

	return get_warehouse_account_map(fx.COMPANY)[fx.KITCHEN].account


class TestSides(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		setup_sides()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.db.set_single_value("Resto Settings", "allow_side_changes", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "allow_side_changes", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	# ------------------------------------------------------------------ helpers

	def order_with(self, *rows):
		order = floor.create_order(order_type="Takeaway", customer_name="Sides Guest")
		floor.add_items(order, json.dumps([{"item_code": c, "qty": q} for c, q in rows]))
		return order

	def change(self, order, line_names, choice):
		floor.set_sides(order, json.dumps(line_names), json.dumps(choice))

	def pay(self, order, lines_=None):
		bill = floor.get_bill(order, json.dumps(lines_) if lines_ else None)
		result = floor.settle(
			order,
			json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]),
			json.dumps(lines_) if lines_ else None,
		)
		return frappe.get_doc("Sales Invoice", result["sales_invoice"])

	def assert_accounts_match_stock(self, si):
		"""Stock account moves by exactly the stock ledger value; every dish row carries its cost."""
		value = sle_value(si.name)
		gl = gl_by_account(si.name)
		self.assertAlmostEqual(sum(gl.values()), 0, 2)
		self.assertAlmostEqual(gl.get(stock_account(), 0), value, 2)
		costs = cost_by_invoice_row({"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date})
		self.assertAlmostEqual(sum(costs.get(r.name, 0) for r in si.items), -value, 2)
		return value

	def swapped_plate(self, qty=2):
		"""A plate with two tahini instead of its garlic sauce, and no rice."""
		order = self.order_with(("DISH-PLATE", qty))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}, RICE: {}})
		return order, line

	# ------------------------------------------------------------------ defaults

	def test_default_dish_deducts_its_recipe(self):
		order = self.order_with(("DISH-PLATE", 2))
		line = lines(order)[0]
		self.assertFalse(line.sides)
		self.assertFalse(line.sides_text)  # the kitchen reads only what differs from the recipe
		floor.send_to_kitchen(order)
		before = bins()
		si = self.pay(order)
		row = si.items[0]
		self.assertFalse(row.resto_sides)
		self.assertEqual(packed(si, row), {"RAW-BUN": 2, "SIDE-RICE": 0.4, "SAUCE-GARLIC": 2})
		self.assertEqual(moved(before), {"RAW-BUN": 2, "SIDE-RICE": 0.4, "SAUCE-GARLIC": 2})
		self.assert_accounts_match_stock(si)

	def test_choosing_the_recipe_sides_stores_nothing(self):
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-GARLIC": 1}, RICE: {"SIDE-RICE": 1}})
		self.assertFalse(lines(order)[0].sides)

	# ------------------------------------------------------------------ one line

	def test_swapped_side_is_deducted_and_costed_on_the_dish(self):
		order, line = self.swapped_plate(2)
		row = lines(order)[0]
		self.assertIn("Sauce Tahini ×2", row.sides_text)
		self.assertIn("Side Rice", row.sides_text)  # "without Side Rice"
		tickets = floor.send_to_kitchen(order)
		ticket_text = frappe.get_all("Resto Kitchen Ticket Item", filters={"parent": ("in", tickets)}, pluck="sides_text")
		self.assertEqual(ticket_text, [row.sides_text])
		self.assertEqual(lines(order)[0].sides_text, row.sides_text)  # kept on the sent line

		before = bins()
		si = self.pay(order)
		dish_row = si.items[0]
		self.assertEqual(json.loads(dish_row.resto_sides), {"SAUCE-TAHINI": 2})
		self.assertEqual(packed(si, dish_row), {"RAW-BUN": 2, "SAUCE-TAHINI": 4})
		self.assertEqual(moved(before), {"RAW-BUN": 2, "SAUCE-TAHINI": 4})
		self.assertTrue(all(i.item_code != "SAUCE-TAHINI" for i in si.items))  # never its own line
		value = self.assert_accounts_match_stock(si)
		self.assertLess(value, 0)

		# Food Cost expects the main ingredients plus the sides served.
		from resto.resto.costing import get_unit_cost
		from resto.resto.report_utils import changed_sides_costs

		main = frappe.db.get_value("Resto Recipe", {"item": "DISH-PLATE", "is_active": 1}, "main_cost")
		self.assertAlmostEqual(changed_sides_costs([dish_row.name])[dish_row.name], flt(main) + 2 * flt(get_unit_cost("SAUCE-TAHINI")), 6)

		# Cancel puts everything back.
		si.cancel()
		self.assertEqual(moved(before), {})

	def test_part_bill_of_a_changed_line(self):
		order, line = self.swapped_plate(3)
		floor.send_to_kitchen(order)
		before = bins()
		first = self.pay(order, {line: 1})
		self.assertEqual(packed(first, first.items[0]), {"RAW-BUN": 1, "SAUCE-TAHINI": 2})
		rest = self.pay(order)
		self.assertEqual(packed(rest, rest.items[0]), {"RAW-BUN": 2, "SAUCE-TAHINI": 4})
		self.assertEqual(moved(before), {"RAW-BUN": 3, "SAUCE-TAHINI": 6})
		self.assert_accounts_match_stock(first)
		self.assert_accounts_match_stock(rest)

	# ------------------------------------------------------------------ several lines

	def test_shared_sides_are_split_over_the_dishes(self):
		order = self.order_with(("DISH-PLATE", 1), ("DISH-GRILL", 1))
		plate, grill = (d.name for d in lines(order))
		self.assertRaises(frappe.ValidationError, self.change, order, [plate, grill], {GROUP: {"SAUCE-TAHINI": 5}})
		self.change(order, [plate, grill], {GROUP: {"SAUCE-TAHINI": 3}})
		rows = lines(order)
		self.assertIn("Sauce Tahini ×3", rows[0].sides_text)
		self.assertIn("Dish Plate", rows[1].sides_text)  # "Test Sauces with Dish Plate"
		floor.send_to_kitchen(order)
		before = bins()
		si = self.pay(order)
		by_item = {r.item_code: r for r in si.items}
		self.assertEqual(packed(si, by_item["DISH-PLATE"]), {"RAW-BUN": 1, "SIDE-RICE": 0.2, "SAUCE-TAHINI": 1.5})
		self.assertEqual(packed(si, by_item["DISH-GRILL"]), {"RAW-BEEF": 0.2, "SAUCE-TAHINI": 1.5})
		self.assertEqual(moved(before), {"RAW-BUN": 1, "SIDE-RICE": 0.2, "RAW-BEEF": 0.2, "SAUCE-TAHINI": 3})
		costs = cost_by_invoice_row({"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date})
		self.assertGreater(costs[by_item["DISH-PLATE"].name], 0)
		self.assertGreater(costs[by_item["DISH-GRILL"].name], 0)
		self.assert_accounts_match_stock(si)

	# ------------------------------------------------------------------ rules

	def test_limits_and_unknown_choices_are_refused(self):
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		for bad in (
			{GROUP: {"SAUCE-TAHINI": 3}},  # more than the group's 2 per dish
			{GROUP: {"SIDE-RICE": 1}},  # not an option of the group
			{"item:RAW-BUN": {"RAW-BUN": 1}},  # a main ingredient is not a side
			{GROUP: {"SAUCE-TAHINI": -1}},
			{RICE: {"SIDE-RICE": 2}},  # a side without a group: kept or left out
		):
			self.assertRaises(frappe.ValidationError, self.change, order, [line], bad)
		self.assertFalse(lines(order)[0].sides)

	def test_switch_off_blocks_changes(self):
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		frappe.db.set_single_value("Resto Settings", "allow_side_changes", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		self.assertRaises(frappe.ValidationError, self.change, order, [line], {GROUP: {"SAUCE-TAHINI": 1}})
		doc = frappe.get_doc("Resto Order", order)
		doc.items[0].sides = json.dumps({GROUP: {"items": {"SAUCE-TAHINI": 1}}})
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_sent_line_cannot_change(self):
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		floor.send_to_kitchen(order)
		self.assertRaises(frappe.ValidationError, self.change, order, [line], {GROUP: {"SAUCE-TAHINI": 1}})

	def test_receipt_shows_sides_only_when_asked(self):
		order, line = self.swapped_plate(1)
		floor.send_to_kitchen(order)
		text = lines(order)[0].sides_text
		si = self.pay(order, {line: 1})
		self.assertNotIn(text, si.items[0].description)
		self.assertTrue(si.items[0].resto_sides)  # always on the invoice itself

		order2, line2 = self.swapped_plate(1)
		floor.set_print_sides(order2, 1)
		floor.send_to_kitchen(order2)
		si2 = self.pay(order2)
		self.assertIn(lines(order2)[0].sides_text, si2.items[0].description)

	# ------------------------------------------------------------------ returns

	def test_full_return_of_a_swapped_side(self):
		order, line = self.swapped_plate(2)
		floor.send_to_kitchen(order)
		before = bins()
		si = self.pay(order)
		sold = sle_value(si.name)

		ret = make_sales_return(si.name)
		ret.insert(ignore_permissions=True)
		self.assertEqual(packed(ret, ret.items[0]), {"RAW-BUN": -2, "SAUCE-TAHINI": -4})
		ret.submit()
		self.assertEqual(moved(before), {})  # all back, garlic and rice never touched
		self.assertTrue(all(p.return_against_packed_item for p in ret.packed_items))
		self.assertAlmostEqual(sle_value(ret.name), -sold, 2)  # back at the cost it went out at
		gl = gl_by_account(ret.name)
		self.assertAlmostEqual(gl.get(stock_account(), 0), -sold, 2)
		costs = cost_by_invoice_row({"company": fx.COMPANY, "from_date": nowdate(), "to_date": nowdate()})
		self.assertAlmostEqual(costs.get(si.items[0].name, 0) + costs.get(ret.items[0].name, 0), 0, 2)
		# Cancelling the return takes the stock out again.
		ret.cancel()
		self.assertEqual(moved(before), {"RAW-BUN": 2, "SAUCE-TAHINI": 4})

	def test_return_must_be_linked_to_the_sold_row(self):
		order, line = self.swapped_plate(1)
		floor.send_to_kitchen(order)
		si = self.pay(order)
		ret = make_sales_return(si.name)
		ret.items[0].sales_invoice_item = None
		self.assertRaises(frappe.ValidationError, ret.insert, ignore_permissions=True)

	def test_sides_served_cannot_be_typed_on_an_invoice(self):
		order, line = self.swapped_plate(1)
		from resto.resto import billing

		si = billing.build_invoice(frappe.get_doc("Resto Order", order))
		si.flags.resto_billing = False
		self.assertRaises(frappe.ValidationError, si.insert, ignore_permissions=True)

	def test_part_returns_of_a_swapped_side(self):
		order, line = self.swapped_plate(2)
		floor.send_to_kitchen(order)
		before = bins()
		si = self.pay(order)
		sold = sle_value(si.name)

		def return_one():
			ret = make_sales_return(si.name)
			ret.items[0].qty = -1
			ret.calculate_taxes_and_totals()
			for p in ret.payments:
				p.amount = p.base_amount = 0
			ret.payments[0].amount = ret.payments[0].base_amount = flt(ret.rounded_total or ret.grand_total)
			ret.insert(ignore_permissions=True)
			self.assertEqual(packed(ret, ret.items[0]), {"RAW-BUN": -1, "SAUCE-TAHINI": -2})
			ret.submit()
			return ret

		first = return_one()
		self.assertEqual(moved(before), {"RAW-BUN": 1, "SAUCE-TAHINI": 2})
		self.assertAlmostEqual(sle_value(first.name), -sold / 2, 2)
		second = return_one()
		self.assertEqual(moved(before), {})
		self.assertAlmostEqual(sle_value(first.name) + sle_value(second.name), -sold, 2)

	# ------------------------------------------------------------------ shared choices change

	def shared_pair(self, sauces=3):
		order = self.order_with(("DISH-PLATE", 1), ("DISH-GRILL", 1))
		plate, grill = (d.name for d in lines(order))
		self.change(order, [plate, grill], {GROUP: {"SAUCE-TAHINI": sauces}})
		return order, plate, grill

	def tahini(self, order):
		out = {}
		for d in lines(order):
			if d.kitchen_status == "Void":
				continue
			choice = json.loads(d.sides) if d.sides else {}
			out[d.item_code] = flt((choice.get(GROUP) or {}).get("items", {}).get("SAUCE-TAHINI")) * flt(d.qty)
		return out

	def test_removed_dish_leaves_the_shared_total_to_the_other(self):
		order, plate, grill = self.shared_pair(3)
		floor.remove_line(order, grill)
		self.assertAlmostEqual(self.tahini(order)["DISH-PLATE"], 3, 6)
		self.assertNotIn('"set"', lines(order)[0].sides or "")

	def test_quantity_change_shares_the_total_again(self):
		order, plate, grill = self.shared_pair(3)
		floor.update_line(order, plate, qty=2)
		shares = self.tahini(order)
		self.assertAlmostEqual(shares["DISH-PLATE"] + shares["DISH-GRILL"], 3, 6)
		self.assertAlmostEqual(shares["DISH-PLATE"], 2, 6)

	def test_removing_a_dish_moves_its_share_to_a_sent_one(self):
		order, plate, grill = self.shared_pair(2)
		# Send only the plate: its ticket says the 2 sauces go with it.
		floor.update_line(order, grill, hold=1)
		floor.send_to_kitchen(order)
		floor.remove_line(order, grill)
		before = bins()
		si = self.pay(order)
		self.assertEqual(packed(si, si.items[0]).get("SAUCE-TAHINI"), 2)
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 2)

	def test_whole_units_are_shared_in_whole_units(self):
		frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 1)
		frappe.clear_document_cache("UOM", "Nos")
		try:
			order, plate, grill = self.shared_pair(3)
			shares = self.tahini(order)
			self.assertEqual(sorted(shares.values()), [1, 2])
			floor.send_to_kitchen(order)
			si = self.pay(order)
			self.assertEqual(sum(packed(si, r).get("SAUCE-TAHINI", 0) for r in si.items), 3)
			# Half a cup for one dish is refused when it is chosen, not at the cashier.
			order2 = self.order_with(("DISH-PLATE", 1))
			line = lines(order2)[0].name
			self.assertRaises(frappe.ValidationError, self.change, order2, [line], {GROUP: {"SAUCE-TAHINI": 0.5}})
		finally:
			frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 0)
			frappe.clear_document_cache("UOM", "Nos")

	def test_sent_line_keeps_what_it_was_served_with(self):
		order, line = self.swapped_plate(1)
		floor.send_to_kitchen(order)
		row = frappe.get_all("Resto Side Option", filters={"parent": GROUP, "item": "SAUCE-TAHINI"}, fields=["name", "portion_qty"])[0]
		frappe.db.set_value("Resto Side Option", row.name, "portion_qty", 5)
		frappe.clear_document_cache("Resto Side Group", GROUP)
		try:
			si = self.pay(order)
			self.assertEqual(packed(si, si.items[0]).get("SAUCE-TAHINI"), 2)  # as sent: 2 portions of 1
		finally:
			frappe.db.set_value("Resto Side Option", row.name, "portion_qty", row.portion_qty)
			frappe.clear_document_cache("Resto Side Group", GROUP)

	# ------------------------------------------------------------------ fail closed

	def test_wrong_packed_rows_stop_the_bill(self):
		order, line = self.swapped_plate(1)
		doc = frappe.get_doc("Resto Order", order)
		from resto.resto import billing

		si = billing.build_invoice(doc)
		si.insert(ignore_permissions=True)
		tahini = next(p for p in si.packed_items if p.item_code == "SAUCE-TAHINI")
		tahini.qty = 1
		self.assertRaises(frappe.ValidationError, sides.check_invoice, si)
		si.delete(ignore_permissions=True)

	def test_bundle_that_differs_from_the_recipe_is_refused(self):
		row = frappe.get_all(
			"Product Bundle Item", filters={"parent": "DISH-PLATE", "item_code": "RAW-BUN"}, fields=["name", "qty"]
		)[0]
		frappe.db.set_value("Product Bundle Item", row.name, "qty", 2)
		try:
			self.assertRaises(frappe.ValidationError, sides.dish_components, "DISH-PLATE", {})
		finally:
			frappe.db.set_value("Product Bundle Item", row.name, "qty", row.qty)
		self.assertEqual(sides.dish_components("DISH-PLATE", {})["RAW-BUN"], 1)

	# ------------------------------------------------------------------ void

	def test_void_after_cooking_wastes_the_sides_served(self):
		order, line = self.swapped_plate(1)
		floor.send_to_kitchen(order)
		before = bins()
		floor.void_line(order, line, "Dropped", cooked=1)
		self.assertEqual(moved(before), {"RAW-BUN": 1, "SAUCE-TAHINI": 2})
