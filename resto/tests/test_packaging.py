"""Packaging and automatic consumables (D-217, D-224, resto/resto/packaging.py).

Each dish takes the packaging rows of its recipe ticked for the order's type (takeaway and delivery
by default); each order takes the Automatic Consumables of Resto Settings ticked for its type, once
per order or once per guest, plus what the cashier added. Stock, cost of goods sold, split bills and
returns must stay exact. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_packaging
"""

import json

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from resto.resto import billing, floor, packaging
from resto.resto.report_utils import cost_by_invoice_row, packaging_cost, sold_rows
from resto.tests import fixtures as fx

WHOLE = "Pack Unit"
WATCH = ("PACK-BOX", "PACK-BAG", "PACK-NAPKIN", "PACK-WIPE", "PACK-CUP", "RAW-BUN", "RAW-BEEF")
ALL_TYPES = {"dine_in": 1, "takeaway": 1, "delivery": 1}
DINE_IN = {"dine_in": 1, "takeaway": 0, "delivery": 0}


def setup_packaging():
	if not frappe.db.exists("UOM", WHOLE):
		fx.doc("UOM", WHOLE, uom_name=WHOLE)
	frappe.db.set_value("UOM", WHOLE, "must_be_whole_number", 1)
	for code, rate in (("PACK-BOX", 2), ("PACK-BAG", 1), ("PACK-NAPKIN", 0.1), ("PACK-WIPE", 0.5), ("PACK-CUP", 3), ("PACK-SHORT", 1)):
		fx.item(code, WHOLE, group="Resto Raw", valuation=rate)
	fx.item("PACK-BUYONLY", WHOLE, group="Resto Raw", valuation=1, sales=0)
	for code, rate in (("DISH-BOXED", 80), ("DISH-COMBO", 150), ("DISH-HALFBOX", 40), ("DISH-SOUP", 30)):
		fx.item(code, stock=0, group="Resto Food")
		fx.price(code, rate)
	dish("DISH-BOXED", [("RAW-BUN", 1)], [("PACK-BOX", 1, {})])
	# A dish that holds DISH-BOXED: the inner dish never brings its box.
	dish("DISH-COMBO", [("DISH-BOXED", 1), ("RAW-BEEF", 0.1)], [])
	# Two of these share a box.
	dish("DISH-HALFBOX", [("RAW-BUN", 1)], [("PACK-BOX", 0.5, {})])
	# Served in its cup in the hall too.
	dish("DISH-SOUP", [("RAW-BUN", 1)], [("PACK-CUP", 1, ALL_TYPES)])
	fx.receive(
		fx.KITCHEN,
		[
			("PACK-BOX", 100, 2),
			("PACK-BAG", 100, 1),
			("PACK-NAPKIN", 500, 0.1),
			("PACK-WIPE", 200, 0.5),
			("PACK-CUP", 100, 3),
			("RAW-BUN", 80, 5),
			("RAW-BEEF", 10, 300),
		],
	)


def dish(code, mains, packs):
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
			"packaging": [{"item_code": c, "qty": q, "uom": uom(c), **flags} for c, q, flags in packs],
		}
	)
	r.insert(ignore_permissions=True)
	return r


def bins():
	return {c: fx.bin_qty(c) for c in WATCH}


def moved(before):
	now = bins()
	return {c: round(before[c] - now[c], 6) for c in WATCH if abs(before[c] - now[c]) > 1e-9}


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


def packaging_rows(si):
	out = {}
	for d in si.items:
		if d.resto_packaging:
			key = (d.item_code, d.resto_packaging)
			out[key] = round(out.get(key, 0) + flt(d.qty), 6)
	return out


def row(item_code, qty, basis="Per Order", can_add_more=0, types=None):
	return {"item_code": item_code, "qty": qty, "basis": basis, "can_add_more": can_add_more, **(types or {"dine_in": 0, "takeaway": 1, "delivery": 1})}


STANDARD = [
	row("PACK-BAG", 1, can_add_more=1),
	row("PACK-NAPKIN", 2),
	row("PACK-WIPE", 1, basis="Per Guest", types=DINE_IN),
]


class TestPackaging(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		setup_packaging()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._kept = {
			("Resto Settings", "require_pos_shift"): frappe.db.get_single_value("Resto Settings", "require_pos_shift"),
			("Selling Settings", "validate_selling_price"): frappe.db.get_single_value("Selling Settings", "validate_selling_price"),
			("Stock Settings", "allow_negative_stock"): frappe.db.get_single_value("Stock Settings", "allow_negative_stock"),
		}
		self._rows = [
			{f: r.get(f) for f in ("item_code", "qty", "basis", "can_add_more", "dine_in", "takeaway", "delivery")}
			for r in frappe.get_single("Resto Settings").get("consumables") or []
		]
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.db.set_single_value("Selling Settings", "validate_selling_price", 0)
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
		self.consumables(STANDARD)
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")

	def tearDown(self):
		frappe.set_user("Administrator")
		for (doctype, field), value in self._kept.items():
			frappe.db.set_single_value(doctype, field, value)
		self.consumables(self._rows)

	def consumables(self, rows):
		settings = frappe.get_single("Resto Settings")
		settings.set("consumables", rows)
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.clear_document_cache("Resto Settings", "Resto Settings")

	def order(self, order_type, *rows, guests=1, send=True):
		if order_type == "Dine In":
			name = floor.create_order(order_type="Dine In", table="T1", guests=guests)
		elif order_type == "Delivery":
			name = floor.create_order(order_type="Delivery", customer_name="Pack Guest", phone="01000000077", address="1 Test St", new_customer=1)
		else:
			name = floor.create_order(order_type="Takeaway", customer_name="Pack Guest")
		floor.add_items(name, json.dumps([{"item_code": c, "qty": q} for c, q in rows]))
		if send:
			floor.send_to_kitchen(name)
		return name

	def pay(self, order, lines_=None):
		bill = floor.get_bill(order, json.dumps(lines_) if lines_ else None)
		result = floor.settle(
			order,
			json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]),
			json.dumps(lines_) if lines_ else None,
		)
		return frappe.get_doc("Sales Invoice", result["sales_invoice"]), bill

	def assert_exact(self, si):
		"""The stock account moves by exactly the stock ledger value, and every line carries its cost."""
		value = sle_value(si.name)
		gl = gl_by_account(si.name)
		self.assertAlmostEqual(sum(gl.values()), 0, 2)
		self.assertAlmostEqual(gl.get(stock_account(), 0), value, 2)
		costs = cost_by_invoice_row({"company": fx.COMPANY, "from_date": si.posting_date, "to_date": si.posting_date})
		self.assertAlmostEqual(sum(costs.get(r.name, 0) for r in si.items), -value, 2)
		return value

	# ------------------------------------------------------------------ order types and guests

	def test_dine_in_takes_per_guest_rows_and_dine_in_packaging_only(self):
		order = self.order("Dine In", ("DISH-BOXED", 2), ("DISH-SOUP", 1), guests=3)
		before = bins()
		si, _bill = self.pay(order)
		# The wipe is per guest on dine-in; the soup's cup is ticked for dine-in; the box and the
		# takeaway bag and napkins are not.
		self.assertEqual(packaging_rows(si), {("PACK-WIPE", "Order"): 3, ("PACK-CUP", "Dish"): 1})
		self.assertEqual(moved(before), {"RAW-BUN": 3, "PACK-WIPE": 3, "PACK-CUP": 1})
		value = self.assert_exact(si)
		self.assertAlmostEqual(-value, 3 * 5 + 3 * 0.5 + 1 * 3, 2)

	def test_guests_change_the_per_guest_quantity(self):
		order = self.order("Dine In", ("DISH-BOXED", 1), guests=1)
		floor.update_order_info(order, guests=5)
		si, _bill = self.pay(order)
		self.assertEqual(packaging_rows(si), {("PACK-WIPE", "Order"): 5})

	def test_takeaway_takes_per_dish_and_per_order_rows(self):
		order = self.order("Takeaway", ("DISH-BOXED", 3), ("DISH-SOUP", 1))
		before = bins()
		si, bill = self.pay(order)
		self.assertEqual(
			packaging_rows(si),
			{("PACK-BOX", "Dish"): 3, ("PACK-CUP", "Dish"): 1, ("PACK-BAG", "Order"): 1, ("PACK-NAPKIN", "Order"): 2},
		)
		self.assertTrue(all(flt(d.rate) == 0 and flt(d.amount) == 0 for d in si.items if d.resto_packaging))
		self.assertEqual(moved(before), {"RAW-BUN": 4, "PACK-BOX": 3, "PACK-CUP": 1, "PACK-BAG": 1, "PACK-NAPKIN": 2})
		# The guest's bill is the food only; the packaging is in the cost of goods sold.
		self.assertEqual(sorted(i["item_code"] for i in bill["items"]), ["DISH-BOXED", "DISH-SOUP"])
		self.assertAlmostEqual(flt(si.net_total), 3 * 80 + 30, 2)
		value = self.assert_exact(si)
		self.assertAlmostEqual(-value, 4 * 5 + 3 * 2 + 3 + 1 + 2 * 0.1, 2)
		# Reports: packaging is not an item sold, and its cost is shown apart.
		f = frappe._dict(company=fx.COMPANY, from_date=nowdate(), to_date=nowdate())
		self.assertFalse([r for r in sold_rows(f) if r.invoice == si.name and r.item_code.startswith("PACK-")])
		self.assertGreaterEqual(packaging_cost(f), 3 * 2 + 3 + 1 + 0.2 - 0.01)

	def test_delivery_takes_the_same(self):
		order = self.order("Delivery", ("DISH-BOXED", 1))
		before = bins()
		si, _bill = self.pay(order)
		self.assertEqual(
			packaging_rows(si),
			{("PACK-BOX", "Dish"): 1, ("PACK-BAG", "Order"): 1, ("PACK-NAPKIN", "Order"): 2},
		)
		self.assertEqual(moved(before), {"RAW-BUN": 1, "PACK-BOX": 1, "PACK-BAG": 1, "PACK-NAPKIN": 2})
		self.assert_exact(si)

	def test_the_order_type_is_read_when_billed(self):
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		doc = frappe.get_doc("Resto Order", order)
		self.assertTrue([d for d in billing.build_invoice(doc).items if d.resto_packaging == "Dish"])
		doc.order_type = "Dine In"  # changed before it is paid
		rows = packaging_rows(billing.build_invoice(doc))
		self.assertNotIn(("PACK-BOX", "Dish"), rows)
		self.assertNotIn(("PACK-BAG", "Order"), rows)
		self.assertEqual(rows.get(("PACK-WIPE", "Order")), 1)

	def test_the_order_type_is_kept_once_part_is_billed(self):
		order = self.order("Takeaway", ("DISH-BOXED", 2))
		line = frappe.get_doc("Resto Order", order).items[0].name
		self.pay(order, {line: 1})
		doc = frappe.get_doc("Resto Order", order)
		doc.order_type = "Delivery"
		self.assertRaises(frappe.ValidationError, doc.save, ignore_permissions=True)

	def test_a_short_item_is_shown_on_the_bill_before_the_payment(self):
		self.consumables([*STANDARD, row("PACK-SHORT", 1)])
		frappe.db.set_value("Item", "PACK-SHORT", "allow_negative_stock", 0)
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		self.assertIn("Pack Short", floor.get_bill(order)["stock_warning"] or "")

	def test_a_dish_inside_another_dish_brings_no_box(self):
		order = self.order("Takeaway", ("DISH-COMBO", 1))
		si, _bill = self.pay(order)
		self.assertNotIn(("PACK-BOX", "Dish"), packaging_rows(si))
		self.assertIn(("PACK-BAG", "Order"), packaging_rows(si))
		self.assert_exact(si)

	# ------------------------------------------------------------------ returns

	def test_a_return_never_restocks_packaging(self):
		order = self.order("Takeaway", ("DISH-BOXED", 2))
		before = bins()
		si, _bill = self.pay(order)
		# A return of the packaging alone is refused.
		only = make_sales_return(si.name)
		only.set("items", [d for d in only.items if d.resto_packaging])
		self.assertRaises(frappe.ValidationError, only.insert, ignore_permissions=True)
		# A full return takes the dishes back, never the packaging.
		ret = make_sales_return(si.name)
		ret.insert(ignore_permissions=True)
		self.assertFalse([d for d in ret.items if d.item_code.startswith("PACK-")])
		ret.submit()
		self.assertEqual(moved(before), {"PACK-BOX": 2, "PACK-BAG": 1, "PACK-NAPKIN": 2})
		gl = gl_by_account(ret.name)
		self.assertAlmostEqual(sum(gl.values()), 0, 2)
		self.assertAlmostEqual(gl.get(stock_account(), 0), sle_value(ret.name), 2)

	def test_a_return_built_without_the_flag_still_keeps_the_packaging(self):
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		si, _bill = self.pay(order)
		ret = make_sales_return(si.name)
		for d in ret.items:
			d.resto_packaging = None  # a return made on another screen does not copy the flag
		# A row naming no original row, of an item that is only packaging on the original bill.
		ret.items[-1].sales_invoice_item = None
		ret.insert(ignore_permissions=True)
		self.assertEqual([d.item_code for d in ret.items], ["DISH-BOXED"])

	# ------------------------------------------------------------------ split bills

	def test_split_bill_adds_up_and_takes_the_order_rows_once(self):
		order = self.order("Takeaway", ("DISH-BOXED", 3))
		line = frappe.get_doc("Resto Order", order).items[0].name
		before = bins()
		first, _ = self.pay(order, {line: 1.5})
		second, _ = self.pay(order)
		# Whole boxes: 1.5 dishes take 2 (rounded up), the rest takes 1; three in all.
		self.assertEqual(packaging_rows(first), {("PACK-BOX", "Dish"): 2, ("PACK-BAG", "Order"): 1, ("PACK-NAPKIN", "Order"): 2})
		self.assertEqual(packaging_rows(second), {("PACK-BOX", "Dish"): 1})
		self.assertEqual(moved(before), {"RAW-BUN": 3, "PACK-BOX": 3, "PACK-BAG": 1, "PACK-NAPKIN": 2})
		self.assert_exact(first)
		self.assert_exact(second)

	def test_a_part_cancelled_and_billed_again_takes_no_extra_box(self):
		# Half a box per dish: three dishes take two boxes, however the bill is split.
		order = self.order("Takeaway", ("DISH-HALFBOX", 3))
		line = frappe.get_doc("Resto Order", order).items[0].name
		before = bins()
		parts = [self.pay(order, {line: 1})[0] for _i in range(3)]
		self.assertEqual([packaging_rows(p).get(("PACK-BOX", "Dish"), 0) for p in parts], [1, 0, 1])
		# Fresh from the database: cancelling an earlier part reposts the later stock entries,
		# which ERPNext stamps on their invoices.
		frappe.get_doc("Sales Invoice", parts[0].name).cancel()
		again, _ = self.pay(order)
		self.assertEqual(packaging_rows(again).get(("PACK-BOX", "Dish"), 0), 1)
		self.assertEqual(moved(before)["PACK-BOX"], 2)
		frappe.get_doc("Sales Invoice", parts[1].name).cancel()
		again, _ = self.pay(order)
		self.assertEqual(packaging_rows(again).get(("PACK-BOX", "Dish"), 0), 0)
		self.assertEqual(moved(before)["PACK-BOX"], 2)
		self.assertEqual(moved(before)["PACK-BAG"], 1, "the per-order rows are taken once in all")

	def test_cancelled_bill_gives_the_packaging_back_and_bills_it_again(self):
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		before = bins()
		si, _bill = self.pay(order)
		si.cancel()
		self.assertEqual(moved(before), {})
		again, _bill = self.pay(order)
		self.assertEqual(packaging_rows(again), {("PACK-BOX", "Dish"): 1, ("PACK-BAG", "Order"): 1, ("PACK-NAPKIN", "Order"): 2})

	# ------------------------------------------------------------------ the cashier's extra

	def test_cashier_adds_a_second_bag(self):
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		floor.set_extra_packaging(order, "PACK-BAG", 1)
		# Only the rows marked for it can be added.
		self.assertRaises(frappe.ValidationError, floor.set_extra_packaging, order, "PACK-NAPKIN", 1)
		before = bins()
		si, _bill = self.pay(order)
		self.assertEqual(packaging_rows(si)[("PACK-BAG", "Order")], 2)
		self.assertEqual(moved(before)["PACK-BAG"], 2)

	def test_the_extra_is_for_the_order_types_ticked_and_is_closed_once_billed(self):
		hall = self.order("Dine In", ("DISH-BOXED", 1))
		self.assertRaises(frappe.ValidationError, floor.set_extra_packaging, hall, "PACK-BAG", 1)
		order = self.order("Takeaway", ("DISH-BOXED", 2))
		floor.set_extra_packaging(order, "PACK-BAG", 2)
		line = frappe.get_doc("Resto Order", order).items[0].name
		self.pay(order, {line: 1})  # the order is still open: its order rows are billed
		self.assertRaises(frappe.ValidationError, floor.set_extra_packaging, order, "PACK-BAG", 0)

	def test_the_extra_cannot_be_written_around_the_api(self):
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		doc = frappe.get_doc("Resto Order", order)
		doc.extra_packaging = json.dumps({"RAW-BEEF": 5})
		doc.save(ignore_permissions=True)
		self.assertFalse(frappe.db.get_value("Resto Order", order, "extra_packaging"))
		# Whatever is stored, only the rows that may be added count, at most MAX_EXTRA each.
		frappe.db.set_value("Resto Order", order, "extra_packaging", json.dumps({"RAW-BEEF": 5, "PACK-BAG": 999}))
		got = packaging.per_order(frappe.get_doc("Resto Order", order))
		self.assertNotIn("RAW-BEEF", got)
		self.assertEqual(got["PACK-BAG"], 1 + packaging.MAX_EXTRA)
		# Zero always clears, even a row no longer allowed.
		floor.set_extra_packaging(order, "RAW-BEEF", 0)
		self.assertEqual(json.loads(frappe.db.get_value("Resto Order", order, "extra_packaging")), {"PACK-BAG": 999})

	# ------------------------------------------------------------------ refusals

	def test_a_short_item_is_named_before_the_payment(self):
		self.consumables([*STANDARD, row("PACK-SHORT", 1)])
		frappe.db.set_value("Item", "PACK-SHORT", "allow_negative_stock", 0)
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		count = frappe.db.count("Sales Invoice", {"resto_order": order})
		with self.assertRaises(frappe.ValidationError) as e:
			self.pay(order)
		self.assertIn("PACK-SHORT".replace("-", " ").title(), str(e.exception))
		self.assertEqual(frappe.db.count("Sales Invoice", {"resto_order": order}), count)

	def test_the_selling_price_check_is_named(self):
		frappe.db.set_single_value("Selling Settings", "validate_selling_price", 1)
		order = self.order("Takeaway", ("DISH-BOXED", 1))
		self.assertRaises(frappe.ValidationError, billing.build_invoice, frappe.get_doc("Resto Order", order))

	def test_settings_refuse_an_item_that_cannot_be_on_a_bill(self):
		self.assertRaises(frappe.ValidationError, self.consumables, [row("PACK-BUYONLY", 1)])
		self.assertRaises(frappe.ValidationError, self.consumables, [row("PACK-BAG", 1, types={"dine_in": 0, "takeaway": 0, "delivery": 0})])
		self.assertRaises(frappe.ValidationError, self.consumables, [row("DISH-BOXED", 1)])

	def test_packaging_must_be_a_stock_item_that_can_be_sold(self):
		r = frappe.get_doc("Resto Recipe", "DISH-BOXED")
		r.append("packaging", {"item_code": "DISH-COMBO", "qty": 1})
		self.assertRaises(frappe.ValidationError, r.save, ignore_permissions=True)
		r = frappe.get_doc("Resto Recipe", "DISH-BOXED")
		r.append("packaging", {"item_code": "PACK-BUYONLY", "qty": 1})
		self.assertRaises(frappe.ValidationError, r.save, ignore_permissions=True)
		r = frappe.get_doc("Resto Recipe", "DISH-BOXED")
		r.append("packaging", {"item_code": "PACK-BAG", "qty": 1, "dine_in": 0, "takeaway": 0, "delivery": 0})
		self.assertRaises(frappe.ValidationError, r.save, ignore_permissions=True)

	def test_packaging_is_not_in_the_dish_cost(self):
		r = frappe.get_doc("Resto Recipe", "DISH-BOXED")
		self.assertAlmostEqual(flt(r.total_cost), 5, 2)
		self.assertAlmostEqual(flt(r.packaging_cost), 2, 2)
