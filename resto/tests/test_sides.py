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

from resto.resto import approval, floor, sides
from resto.resto.report.resto_voids_and_discounts.resto_voids_and_discounts import execute as voids_report
from resto.resto.report_utils import cost_by_invoice_row
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import MANAGER_USER, _manager

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
	# Extra portions (charged only when Resto Settings says so) are billed as the sauce itself.
	group = frappe.get_doc("Resto Side Group", GROUP)
	for o in group.options:
		o.extra_price = {"SAUCE-GARLIC": 4, "SAUCE-TAHINI": 5}[o.item]
		o.billing_item = o.item
	group.save(ignore_permissions=True)
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
		frappe.db.set_single_value("Resto Settings", "charge_extra_sides", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		frappe.db.set_single_value("Resto Settings", "allow_side_changes", 0)
		frappe.db.set_single_value("Resto Settings", "charge_extra_sides", 0)
		frappe.db.set_single_value("Resto Settings", "free_extras_limit", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def charge(self):
		frappe.db.set_single_value("Resto Settings", "charge_extra_sides", 1)
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

	def test_the_kitchen_can_show_every_dishs_sides(self):
		# D-252: on, the kitchen ticket carries the recipe's sides of an unchanged dish; the line, the
		# bill and the receipt do not. Off (the default): only changed sides.
		def ticket_text(order):
			tickets = floor.send_to_kitchen(order)
			return frappe.get_all("Resto Kitchen Ticket Item", filters={"parent": ("in", tickets)}, pluck="sides_text")

		self.assertEqual(ticket_text(self.order_with(("DISH-PLATE", 1))), [None])
		frappe.db.set_single_value("Resto Settings", "show_sides_in_kitchen", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		self.addCleanup(frappe.db.set_single_value, "Resto Settings", "show_sides_in_kitchen", 0)
		order = self.order_with(("DISH-PLATE", 2))
		[text] = ticket_text(order)
		self.assertIn("Sauce Garlic", text)
		self.assertIn("Side Rice", text)
		self.assertFalse(lines(order)[0].sides_text)
		si = self.pay(order)
		self.assertFalse(si.items[0].resto_sides_text)
		self.assertNotIn("Side Rice", si.items[0].description or "")
		# A changed dish keeps its own text.
		swapped, _line = self.swapped_plate(1)
		own = lines(swapped)[0].sides_text
		self.assertEqual(ticket_text(swapped), [own])

	def test_the_without_part_stands_out_in_the_kitchen(self):
		# D-251: on the kitchen screen and paper the «without» part is red, bold and larger.
		from resto.resto import kitchen
		from resto.resto.sides import _t, sides_html

		order, _line = self.swapped_plate(1)
		text = lines(order)[0].sides_text
		html = sides_html(text)
		start = html.index('<span class="resto-without">')
		self.assertIn("Side Rice", html[start:])
		self.assertNotIn("Sauce Tahini", html[start:])  # the dish's sides served stay as they were
		# On a line of its own under the rest, with no «|» before it (D-263).
		self.assertIn('<br><span class="resto-without">', html)
		self.assertNotIn("| <span", html)
		head, without = _t("Sides: {0}", ""), _t("without: {0}", "Side Rice")
		self.assertEqual(sides_html(f"{head}Sauce Tahini | {without}"), f'{head}Sauce Tahini<br><span class="resto-without">{without}</span>')
		self.assertEqual(sides_html(f"{head}{without}"), f'<span class="resto-without">{without}</span>')
		self.assertEqual(sides_html(None), "")
		self.assertEqual(sides_html("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;")
		tickets = floor.send_to_kitchen(order)
		self.assertIn('class="resto-without"', frappe.get_print("Resto Kitchen Ticket", tickets[0], "Resto Kitchen Ticket"))
		shown = [d for t in kitchen.get_tickets() for d in t["items"] if t["name"] in tickets]
		self.assertTrue(any('class="resto-without"' in d["sides_html"] for d in shown))

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
		# Sides typed on the form or over REST are not taken: only the floor screen sets them.
		frappe.db.set_single_value("Resto Settings", "allow_side_changes", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		doc = frappe.get_doc("Resto Order", order)
		doc.items[0].sides = json.dumps({GROUP: {"items": {"SAUCE-TAHINI": 1}}})
		doc.save()
		self.assertFalse(lines(order)[0].sides)

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

	def test_copies_show_sides_by_their_own_setting(self):
		from resto.resto import printing

		settings = frappe.get_doc("Resto Settings")
		saved = {
			"cashier_print_method": settings.cashier_print_method,
			"receipt_print_format": settings.receipt_print_format,
			"bill_copies": [(r.label, r.show_sides) for r in settings.bill_copies],
			"receipt_copies": [(r.label, r.show_sides) for r in settings.receipt_copies],
		}
		copies = [{"label": "Customer Copy", "show_sides": 0}, {"label": "Restaurant Copy", "show_sides": 1}]
		settings.cashier_print_method = printing.BROWSER_BUTTON
		settings.receipt_print_format = printing.RECEIPT_FORMAT
		settings.set("bill_copies", copies)
		settings.set("receipt_copies", copies)
		settings.save()
		try:
			order, line = self.swapped_plate(1)
			floor.send_to_kitchen(order)
			text = lines(order)[0].sides_text
			customer, restaurant = floor.request_bill(order)["pages"]
			self.assertNotIn(text, customer)
			self.assertIn(text, restaurant)
			si = self.pay(order)
			self.assertEqual(si.items[0].resto_sides_text, text)
			customer, restaurant = printing.receipt_pages(si.name)
			self.assertIn("Customer Copy", customer)
			self.assertNotIn(text, customer)
			self.assertIn(text, restaurant)
			self.assertIn(order, customer)  # the order number, to match the kitchen tickets

			# The order tick prints the sides on every copy.
			order2, line2 = self.swapped_plate(1)
			floor.set_print_sides(order2, 1)
			floor.send_to_kitchen(order2)
			text2 = lines(order2)[0].sides_text
			for page in printing.receipt_pages(self.pay(order2).name):
				self.assertEqual(page.count(text2), 1)
		finally:
			settings = frappe.get_doc("Resto Settings")
			settings.cashier_print_method = saved["cashier_print_method"]
			settings.receipt_print_format = saved["receipt_print_format"]
			for table in ("bill_copies", "receipt_copies"):
				settings.set(table, [{"label": label, "show_sides": show} for label, show in saved[table]])
			settings.save()


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

	# ------------------------------------------------------------------ paid extra portions

	def test_extra_portions_are_their_own_priced_line(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 2))
		line = lines(order)[0].name
		# The recipe serves one garlic sauce; two tahini is one free swap and one extra per dish.
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}})
		row = lines(order)[0]
		self.assertEqual(flt(row.sides_amount), 5)
		self.assertAlmostEqual(flt(row.amount), 2 * (120 + 5), 2)
		floor.send_to_kitchen(order)
		before = bins()
		si = self.pay(order)
		dish_row = next(r for r in si.items if r.item_code == "DISH-PLATE")
		extra = [r for r in si.items if r.item_code == "SAUCE-TAHINI"]
		self.assertEqual(len(extra), 1)
		self.assertEqual((flt(extra[0].qty), flt(extra[0].rate)), (2, 5))
		self.assertIn("Dish Plate", extra[0].description)
		# The dish packs its free sauce only; the extra line deducts its own two.
		self.assertEqual(packed(si, dish_row), {"RAW-BUN": 2, "SIDE-RICE": 0.4, "SAUCE-TAHINI": 2})
		self.assertEqual(moved(before), {"RAW-BUN": 2, "SIDE-RICE": 0.4, "SAUCE-TAHINI": 4})
		self.assertAlmostEqual(flt(si.net_total), 250, 2)
		self.assert_accounts_match_stock(si)
		costs = cost_by_invoice_row({"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date})
		self.assertGreater(costs.get(extra[0].name, 0), 0)

		# A full return takes the extra line back like any line.
		ret = make_sales_return(si.name)
		ret.insert(ignore_permissions=True)
		ret.submit()
		self.assertEqual(moved(before), {})
		self.assertAlmostEqual(sle_value(ret.name), -sle_value(si.name), 2)

	def test_swap_of_the_same_count_is_free(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 1}})
		self.assertEqual(flt(lines(order)[0].sides_amount), 0)
		floor.send_to_kitchen(order)
		si = self.pay(order)
		self.assertEqual([r.item_code for r in si.items], ["DISH-PLATE"])

	def test_extra_portions_are_free_when_not_charged(self):
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}})
		self.assertEqual(flt(lines(order)[0].sides_amount), 0)
		floor.send_to_kitchen(order)
		si = self.pay(order)
		self.assertEqual([r.item_code for r in si.items], ["DISH-PLATE"])
		self.assertEqual(packed(si, si.items[0]).get("SAUCE-TAHINI"), 2)

	def test_shared_extras_are_one_line(self):
		self.charge()
		order, plate, grill = self.shared_pair(3)  # the two recipes serve 2 sauces: 1 is extra
		floor.send_to_kitchen(order)
		before = bins()
		si = self.pay(order)
		extra = [r for r in si.items if r.item_code == "SAUCE-TAHINI"]
		self.assertEqual(len(extra), 1)
		self.assertAlmostEqual(flt(extra[0].qty), 1, 6)
		self.assertAlmostEqual(flt(extra[0].amount), 5, 2)
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)
		self.assertAlmostEqual(flt(si.net_total), 120 + 160 + 5, 2)
		self.assert_accounts_match_stock(si)

	def test_an_extra_portion_billed_with_its_cup(self):
		# D-265: the billing recipe may hold packaging besides exactly one portion of the option.
		if not frappe.db.exists("Item Group", "Test Packaging"):
			frappe.get_doc(
				{"doctype": "Item Group", "item_group_name": "Test Packaging", "parent_item_group": "All Item Groups"}
			).insert(ignore_permissions=True)
		fx.item("PACK-CUP", "Nos", group="Test Packaging", valuation=1)
		fx.receive(fx.KITCHEN, [("PACK-CUP", 20, 1)])
		for code, mains in (
			("DISH-XT-CUP", [("SAUCE-TAHINI", 1), ("PACK-CUP", 1)]),
			("DISH-XT-RICE", [("SAUCE-TAHINI", 1), ("SIDE-RICE", 0.1)]),
			("DISH-XT-TWO", [("SAUCE-TAHINI", 2), ("PACK-CUP", 1)]),
		):
			fx.item(code, stock=0, group="Resto Food")
			fx.price(code, 5)
			dish(code, mains, [])
		kept = frappe.db.get_single_value("Resto Settings", "packaging_item_group")
		self.addCleanup(self.packaging_group, kept)

		def error(billing):
			frappe.local.resto_extra_billing = {}
			return sides.extra_billing_error("SAUCE-TAHINI", 1, billing)

		self.packaging_group(None)
		self.assertIn("Pack Cup", error("DISH-XT-CUP") or "")  # no packaging group: the cup is not allowed
		self.packaging_group("Test Packaging")
		self.assertIsNone(error("DISH-XT-CUP"))
		self.assertIn("also deducts", error("DISH-XT-RICE") or "")  # another food: refused, named
		self.assertIn("Side Rice", error("DISH-XT-RICE"))
		self.assertIn("is not exactly one portion", error("DISH-XT-TWO") or "")  # two portions: refused

		# The cup is deducted with each extra portion, charged or free.
		group = frappe.get_doc("Resto Side Group", GROUP)
		next(o for o in group.options if o.item == "SAUCE-TAHINI").billing_item = "DISH-XT-CUP"
		frappe.local.resto_extra_billing = {}
		group.save(ignore_permissions=True)
		self.addCleanup(self.bill_tahini_as, "SAUCE-TAHINI")
		self.charge()
		for free in (0, 1):
			order, line = self.tahini_plates(1)
			if free:
				floor.set_extras_free(order, line)
			cup, tahini = fx.bin_qty("PACK-CUP"), fx.bin_qty("SAUCE-TAHINI")
			si = self.pay(order)
			extra = [r for r in si.items if r.item_code == "DISH-XT-CUP"]
			self.assertEqual([(flt(r.qty), flt(r.amount)) for r in extra], [(1, 0 if free else 5)])
			self.assertAlmostEqual(cup - fx.bin_qty("PACK-CUP"), 1, 6)
			self.assertAlmostEqual(tahini - fx.bin_qty("SAUCE-TAHINI"), 2, 6)

	def packaging_group(self, group):
		frappe.db.set_single_value("Resto Settings", "packaging_item_group", group)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.local.resto_extra_billing = {}

	def bill_tahini_as(self, billing):
		group = frappe.get_doc("Resto Side Group", GROUP)
		next(o for o in group.options if o.item == "SAUCE-TAHINI").billing_item = billing
		frappe.local.resto_extra_billing = {}
		group.save(ignore_permissions=True)

	def test_a_portion_is_typed_in_grams(self):
		# D-264: typed in a recipe unit, kept in the stock unit; a unit the item has not is refused.
		from resto.tests.test_staff_meal import small_unit

		sauce = small_unit(self, "SAUCE-DIP-KG", "Kg", "Gram")
		name = "Test Dip Units"
		if frappe.db.exists("Resto Side Group", name):
			frappe.delete_doc("Resto Side Group", name, force=True)
		group = frappe.get_doc(
			{
				"doctype": "Resto Side Group",
				"group_name": name,
				"max_portions": 2,
				"options": [
					{"item": sauce, "entered_qty": 30, "entered_uom": "Gram"},
					{"item": "SAUCE-GARLIC", "portion_qty": 2},  # a row from before: in the stock unit
				],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Resto Side Group", group.name, force=True)
		dip, garlic = group.options
		self.assertEqual((flt(dip.portion_qty, 6), dip.uom), (0.03, "Kg"))
		self.assertEqual((flt(garlic.entered_qty), garlic.entered_uom, flt(garlic.portion_qty)), (2, "Nos", 2))
		self.assertEqual(flt(frappe.db.get_value("Resto Side Option", dip.name, "portion_qty"), 6), 0.03)  # what is deducted
		# Grams of an item counted in pieces: no conversion, refused.
		garlic.entered_uom = "Gram"
		self.assertRaises(frappe.ValidationError, group.save)

	def test_extra_price_needs_an_item_that_deducts_one_portion(self):
		group = frappe.get_doc("Resto Side Group", GROUP)
		row = next(o for o in group.options if o.item == "SAUCE-TAHINI")
		row.entered_qty = 2  # one portion is two stock units: the stock item would deduct one
		self.assertRaises(frappe.ValidationError, group.save)
		group.reload()
		row = next(o for o in group.options if o.item == "SAUCE-TAHINI")
		row.billing_item = None
		self.assertRaises(frappe.ValidationError, group.save)

	def whole_nos(self):
		frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 1)
		frappe.clear_document_cache("UOM", "Nos")
		self.addCleanup(frappe.clear_document_cache, "UOM", "Nos")
		self.addCleanup(frappe.db.set_value, "UOM", "Nos", "must_be_whole_number", 0)

	def test_shared_swap_of_the_same_count_is_free_in_whole_units(self):
		self.charge()
		self.whole_nos()
		order = self.order_with(("DISH-PLATE", 1), ("DISH-GRILL", 1))
		plate, grill = (d.name for d in lines(order))
		# The two recipes serve 2 sauces together: 1 garlic + 1 tahini is a swap, not an extra.
		self.change(order, [plate, grill], {GROUP: {"SAUCE-GARLIC": 1, "SAUCE-TAHINI": 1}})
		rows = lines(order)
		self.assertEqual([flt(d.sides_amount) for d in rows], [0, 0])
		# Each dish gets one sauce, not both on the first.
		per_line = [sum((json.loads(d.sides)[GROUP]["items"]).values()) for d in rows]
		self.assertEqual(per_line, [1, 1])
		floor.send_to_kitchen(order)
		si = self.pay(order)
		self.assertEqual(sorted(r.item_code for r in si.items), ["DISH-GRILL", "DISH-PLATE"])

	def test_shared_extras_in_whole_units_can_be_billed_line_by_line(self):
		self.charge()
		self.whole_nos()
		order, plate, grill = self.shared_pair(3)  # 1 extra tahini for the two dishes
		rows = {d.name: d for d in lines(order)}
		self.assertEqual(sum(flt(d.sides_amount) for d in rows.values()), 5)
		floor.send_to_kitchen(order)
		before = bins()
		# Bill each dish on its own invoice: every extra part is whole, so neither bill is stuck.
		first = self.pay(order, {plate: 1})
		second = self.pay(order, {grill: 1})
		extra = [r for si in (first, second) for r in si.items if r.item_code == "SAUCE-TAHINI"]
		self.assertEqual([flt(r.qty) for r in extra], [1])
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)
		self.assertAlmostEqual(flt(first.net_total) + flt(second.net_total), 120 + 160 + 5, 2)

	def test_billed_line_of_a_set_keeps_what_it_billed(self):
		self.charge()
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before_amount = {d.name: flt(d.sides_amount) for d in lines(order)}
		si = self.pay(order, {plate: 1})
		floor.void_line(order, grill, "Changed mind", cooked=0)
		row = next(d for d in lines(order) if d.name == plate)
		self.assertEqual(flt(row.sides_amount), before_amount[plate])
		self.assertEqual(flt(row.billed_qty), 1)
		self.assertTrue(si.docstatus == 1)

	def test_late_waste_leaves_out_a_share_that_moved(self):
		# Plate and grill share 3 tahini. The grill is voided uncooked: the plate takes all 3. A waste
		# posted later for the grill must not take its old share again.
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before = bins()
		floor.void_line(order, grill, "Changed mind", cooked=0)
		self.assertAlmostEqual(self.tahini(order)["DISH-PLATE"], 3, 6)
		floor.post_cooked_waste(order, json.dumps([grill]))
		self.pay(order)
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)

	def test_cancel_wastes_the_shared_total_with_the_cooked_dish(self):
		# Only the plate was cooked: the kitchen plated the 3 tahini of the set with it.
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before = bins()
		floor.cancel_order(order, "Guest left", cooked=json.dumps([plate]))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)
		self.assertFalse(frappe.db.get_value("Resto Order Item", grill, "void_waste"))

	def test_void_after_cooking_wastes_the_extra_portions(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}, RICE: {}})
		self.assertEqual(flt(lines(order)[0].sides_amount), 5)
		floor.send_to_kitchen(order)
		before = bins()
		floor.void_line(order, line, "Dropped", cooked=1)
		self.assertEqual(moved(before), {"RAW-BUN": 1, "SAUCE-TAHINI": 2})

	def test_part_bill_of_a_line_with_extras(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 2))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}})
		floor.send_to_kitchen(order)
		before = bins()
		first = self.pay(order, {line: 1})
		extra = [r for r in first.items if r.item_code == "SAUCE-TAHINI"]
		self.assertEqual([(flt(r.qty), flt(r.rate)) for r in extra], [(1, 5)])
		self.assertAlmostEqual(flt(first.net_total), 125, 2)
		second = self.pay(order)
		self.assertAlmostEqual(flt(second.net_total), 125, 2)
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 4)

	def test_sent_line_keeps_its_extra_price(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}})
		floor.send_to_kitchen(order)
		row = frappe.get_all("Resto Side Option", filters={"parent": GROUP, "item": "SAUCE-TAHINI"}, fields=["name", "extra_price"])[0]
		frappe.db.set_value("Resto Side Option", row.name, "extra_price", 9)
		frappe.clear_document_cache("Resto Side Group", GROUP)
		frappe.db.set_single_value("Resto Settings", "charge_extra_sides", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		try:
			si = self.pay(order)
			extra = [r for r in si.items if r.item_code == "SAUCE-TAHINI"]
			self.assertEqual([(flt(r.qty), flt(r.rate)) for r in extra], [(1, 5)])
		finally:
			frappe.db.set_value("Resto Side Option", row.name, "extra_price", row.extra_price)
			frappe.clear_document_cache("Resto Side Group", GROUP)

	# ------------------------------------------------------------------ free extras (D-253)

	def tahini_plates(self, qty, send=True):
		"""Plates with two tahini instead of the garlic sauce: one extra tahini per dish at 5."""
		order = self.order_with(("DISH-PLATE", qty))
		line = lines(order)[-1].name
		self.change(order, [line], {GROUP: {"SAUCE-TAHINI": 2}})
		if send:
			floor.send_to_kitchen(order)
		return order, line

	def test_free_extras_bill_at_no_price_with_the_stock_issued(self):
		self.charge()
		order, line = self.tahini_plates(2)
		floor.set_extras_free(order, line)
		row = frappe.get_doc("Resto Order Item", line)
		self.assertEqual((row.sides_free, row.sides_free_by), (1, "Administrator"))
		self.assertEqual((flt(row.sides_amount), flt(row.sides_free_value)), (0, 5))
		self.assertAlmostEqual(flt(row.amount), 240, 2)
		self.assertIn("Free of charge", sides.extras_text(row))
		before = bins()
		si = self.pay(order)
		extra = [r for r in si.items if r.item_code == "SAUCE-TAHINI"]
		self.assertEqual(len(extra), 1)
		self.assertEqual((flt(extra[0].qty), flt(extra[0].rate), flt(extra[0].amount)), (2, 0, 0))
		self.assertEqual((extra[0].resto_free_extra, flt(extra[0].resto_free_value), extra[0].resto_free_by), (1, 10, "Administrator"))
		self.assertIn(sides._t("Free of charge"), extra[0].description)
		# The stock goes out as when it is charged: the dish's free sauce and the two extra.
		self.assertEqual(moved(before), {"RAW-BUN": 2, "SIDE-RICE": 0.4, "SAUCE-TAHINI": 4})
		self.assertAlmostEqual(flt(si.net_total), 240, 2)
		self.assert_accounts_match_stock(si)
		# No income for the free line: the income accounts hold the dishes only.
		gl = gl_by_account(si.name)
		self.assertAlmostEqual(-sum(gl.get(a, 0) for a in {r.income_account for r in si.items}), 240, 2)
		costs = cost_by_invoice_row({"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date})
		self.assertGreater(costs.get(extra[0].name, 0), 0)

		# Resto Voids and Discounts: its own type, at price and at cost.
		_c, data, _m, _ch, summary = voids_report(
			{"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date, "type": "Free Extras"}
		)
		got = [d for d in data if d.sales_invoice == si.name]
		self.assertEqual(len(got), 1)
		self.assertEqual((got[0].item_code, flt(got[0].qty), flt(got[0].value), got[0].given_by), ("SAUCE-TAHINI", 2, 10, "Administrator"))
		self.assertAlmostEqual(flt(got[0].cost), costs[extra[0].name], 2)
		self.assertGreaterEqual(flt(summary[1]["value"]), 10)

		# A return takes the line back at no price, with its stock.
		ret = make_sales_return(si.name)
		ret.insert(ignore_permissions=True)
		ret.submit()
		self.assertEqual(moved(before), {})
		_c, data, *_rest = voids_report({"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date, "type": "Free Extras"})
		self.assertFalse([d for d in data if d.sales_invoice == ret.name])

	def test_free_extras_need_a_pin_above_the_limit(self):
		_manager()
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin("4321")
		frappe.set_user("Administrator")
		self.charge()
		cashier = "cashier@resto.test"

		# No limit set: always a manager's PIN, checked by the server.
		order, line = self.tahini_plates(1)
		frappe.set_user(cashier)
		self.assertRaises(approval.ApprovalRequired, floor.set_extras_free, order, line)
		self.assertRaises(approval.ApprovalRequired, floor.set_extras_free, order, line, 1, MANAGER_USER, "1111")
		self.assertEqual(frappe.db.get_value("Resto Order Item", line, "sides_free"), 0)
		floor.set_extras_free(order, line, 1, MANAGER_USER, "4321")
		self.assertEqual(frappe.db.get_value("Resto Order Item", line, "sides_free_by"), MANAGER_USER)
		# Charged again: back on the bill, no approval needed.
		floor.set_extras_free(order, line, 0)
		row = frappe.get_doc("Resto Order Item", line)
		self.assertEqual((row.sides_free, row.sides_free_by, flt(row.sides_amount), flt(row.amount)), (0, None, 5, 125))
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)

		# Within the limit per order the cashier gives them; above it, the PIN again.
		frappe.db.set_single_value("Resto Settings", "free_extras_limit", 12)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		order, line = self.tahini_plates(2)
		frappe.set_user(cashier)
		floor.set_extras_free(order, line)
		self.assertEqual(frappe.db.get_value("Resto Order Item", line, "sides_free_by"), cashier)
		frappe.set_user("Administrator")
		floor.add_items(order, json.dumps([{"item_code": "DISH-PLATE", "qty": 1}]))
		second = lines(order)[-1].name
		self.change(order, [second], {GROUP: {"SAUCE-TAHINI": 2}})
		frappe.set_user(cashier)
		self.assertRaises(approval.ApprovalRequired, floor.set_extras_free, order, second)  # 10 + 5 > 12
		frappe.set_user("Administrator")

		# The mark is the server's own: a form or REST save cannot set it.
		doc = frappe.get_doc("Resto Order", order)
		doc.items[-1].sides_free = 1
		doc.items[-1].sides_amount = 0
		doc.save(ignore_permissions=True)
		row = frappe.get_doc("Resto Order Item", second)
		self.assertEqual((row.sides_free, flt(row.sides_amount)), (0, 5))

		# An unsent line given free loses the mark when its quantity changes (the value changes).
		frappe.db.set_single_value("Resto Settings", "free_extras_limit", 100)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		floor.set_extras_free(order, second)
		self.assertEqual(frappe.db.get_value("Resto Order Item", second, "sides_free"), 1)
		floor.update_line(order, second, qty=2)
		row = frappe.get_doc("Resto Order Item", second)
		self.assertEqual((row.sides_free, flt(row.sides_amount)), (0, 5))

	def test_free_extras_only_on_a_line_with_charged_extras(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 1))
		line = lines(order)[0].name
		self.assertRaises(frappe.ValidationError, floor.set_extras_free, order, line)
		# Extras shared by several dishes are one bill line: not given free one dish at a time.
		order, plate, grill = self.shared_pair(3)
		for shared in (plate, grill):
			self.assertRaises(frappe.ValidationError, floor.set_extras_free, order, shared)

	def test_recipe_side_in_another_portion_is_not_charged(self):
		self.charge()
		fx.item("DISH-DOUBLE", stock=0, group="Resto Food")
		fx.price("DISH-DOUBLE", 150)
		dish("DISH-DOUBLE", [("RAW-BUN", 1)], [("SAUCE-GARLIC", 2, GROUP)])  # two cups, the group's portion is one
		slot = sides.slots("DISH-DOUBLE")[GROUP]
		self.assertFalse(slot.price)
		order = self.order_with(("DISH-DOUBLE", 1))
		line = lines(order)[0].name
		self.change(order, [line], {GROUP: {"SAUCE-GARLIC": 2}})  # four cups, all on the dish
		self.assertEqual(flt(lines(order)[0].sides_amount), 0)
		self.assertEqual(json.loads(lines(order)[0].sides_qty), {"SAUCE-GARLIC": 4})

	def test_billing_item_must_be_the_option_itself(self):
		group = frappe.get_doc("Resto Side Group", GROUP)
		row = next(o for o in group.options if o.item == "SAUCE-TAHINI")
		row.billing_item = "SAUCE-GARLIC"  # would deduct garlic for tahini served
		self.assertRaises(frappe.ValidationError, group.save)
		self.assertIsNone(sides.extra_billing_error("SAUCE-TAHINI", 1, "SAUCE-TAHINI"))
		self.assertTrue(sides.extra_billing_error("SAUCE-TAHINI", 1, "SAUCE-GARLIC"))

	def test_line_whose_shared_sides_are_all_extras_packs_none_on_the_dish(self):
		self.charge()
		order = self.order_with(("DISH-PLATE", 1))
		row = lines(order)[0]
		row.kitchen_status = "Not Sent"
		# Its share of a set is one tahini, all of it above what the set serves free.
		choice = {
			GROUP: {"items": {"SAUCE-TAHINI": 1}, "x": {"SAUCE-TAHINI": 1}, "set": "t1", "n": 2, "q": 1, "t": {"SAUCE-TAHINI": 3}, "c": 1},
			RICE: {"items": {"SIDE-RICE": 1}},
		}
		sides._apply(row, sides.clean_choice(row.item_code, choice, row.item_name, strict=False))
		self.assertEqual(json.loads(row.sides_qty), {"SIDE-RICE": 0.2})
		self.assertEqual({k: flt(v["portions"]) for k, v in json.loads(row.sides_extras).items()}, {"SAUCE-TAHINI": 1})
		choice[RICE] = {"items": {}}
		sides._apply(row, sides.clean_choice(row.item_code, choice, row.item_name, strict=False))
		self.assertEqual(row.sides_qty, "{}")  # stored: the dish packs no side, the extra line has the tahini

	def test_quantity_change_after_a_shared_dish_was_billed(self):
		self.charge()
		self.whole_nos()
		# Dine in: paying a takeaway sends every line to the kitchen, held ones too.
		order = floor.create_order(order_type="Dine In", table="T6", guests=2)
		floor.add_items(order, json.dumps([{"item_code": "DISH-PLATE", "qty": 1}, {"item_code": "DISH-GRILL", "qty": 2}]))
		plate, grill = (d.name for d in lines(order))
		self.change(order, [plate, grill], {GROUP: {"SAUCE-TAHINI": 4}})
		floor.update_line(order, grill, hold=1)
		floor.send_to_kitchen(order)
		self.pay(order, {plate: 1})
		floor.update_line(order, grill, qty=1)
		row = next(d for d in lines(order) if d.name == grill)
		self.assertFalse(row.sides)  # back to its recipe: no fraction of a cup is left on it
		floor.update_line(order, grill, hold=0)
		floor.send_to_kitchen(order)
		si = self.pay(order)
		self.assertEqual(packed(si, si.items[0]).get("SAUCE-GARLIC"), 1)

	def test_extra_portions_round_down_to_whole_units(self):
		self.charge()
		self.whole_nos()
		order = self.order_with(("DISH-PLATE", 2))
		row = lines(order)[0]
		row.kitchen_status = "Not Sent"
		# 1.5 tahini per dish on two dishes: 1 free each, 1 extra cup for the line, whole.
		sides._apply(row, sides.clean_choice(row.item_code, {GROUP: {"items": {"SAUCE-TAHINI": 1.5}}}, row.item_name, strict=False))
		self.assertAlmostEqual(flt(json.loads(row.sides_extras)["SAUCE-TAHINI"]["portions"]) * 2, 1, 6)
		row.qty = 3
		sides._apply(row, sides.clean_choice(row.item_code, {GROUP: {"items": {"SAUCE-TAHINI": 1.5}}}, row.item_name, strict=False))
		self.assertAlmostEqual(flt(json.loads(row.sides_extras)["SAUCE-TAHINI"]["portions"]) * 3, 1, 6)  # 1.5 → 1 cup billed, the half free

	def test_late_waste_of_the_other_dish_after_cancel_takes_nothing_twice(self):
		# The plate's waste took the 3 tahini of the set; the grill posted later wastes only its own dish.
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before = bins()
		floor.cancel_order(order, "Guest left", cooked=json.dumps([plate]))
		self.assertTrue(floor.post_cooked_waste(order, json.dumps([grill]))[grill])
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)

	def test_cancel_without_ticks_then_a_late_waste_takes_the_set(self):
		# Same as ticking the plate at the cancel: the set's 3 tahini, once.
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before = bins()
		floor.cancel_order(order, "Guest left")
		floor.post_cooked_waste(order, json.dumps([plate]))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)
		floor.post_cooked_waste(order, json.dumps([grill]))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)

	def test_a_cancelled_waste_gives_back_the_shares_it_took(self):
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before = bins()
		floor.cancel_order(order, "Guest left", cooked=json.dumps([plate]))
		frappe.get_doc("Resto Waste", frappe.db.get_value("Resto Order Item", plate, "void_waste")).cancel()
		self.assertFalse(moved(before).get("SAUCE-TAHINI"))
		self.assertNotIn('"w"', frappe.db.get_value("Resto Order Item", grill, "sides"))
		floor.post_cooked_waste(order, json.dumps([grill]))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)

	def test_cancelling_the_first_waste_of_a_set_then_posting_it_again(self):
		# Both cooked: the plate's waste holds the 3 tahini. Corrected (cancelled and posted again): 3.
		order, plate, grill = self.shared_pair(3)
		floor.send_to_kitchen(order)
		before = bins()
		floor.cancel_order(order, "Guest left", cooked=json.dumps([plate, grill]))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)
		frappe.get_doc("Resto Waste", frappe.db.get_value("Resto Order Item", plate, "void_waste")).cancel()
		floor.post_cooked_waste(order, json.dumps([plate]))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 3)

	def test_cancel_without_ticks_then_a_late_waste_with_extras_charged(self):
		self.charge()
		self.test_cancel_without_ticks_then_a_late_waste_takes_the_set()

	def test_part_of_a_line_after_a_void_wastes_its_share_for_those_dishes(self):
		# Charged extras mark shared choices "c": that is not the cancel's "the set was plated at once".
		self.charge()
		order = self.order_with(("DISH-PLATE", 3), ("DISH-GRILL", 1))
		plate, grill = (d.name for d in lines(order))
		self.change(order, [plate, grill], {GROUP: {"SAUCE-TAHINI": 4}})
		floor.send_to_kitchen(order)
		floor.void_line(order, plate, "Dropped", cooked=1)
		frappe.get_doc("Resto Waste", frappe.db.get_value("Resto Order Item", plate, "void_waste")).cancel()
		before = bins()
		floor.post_cooked_waste(order, json.dumps({plate: 1}))
		self.assertEqual(moved(before).get("SAUCE-TAHINI"), 1)
