"""Kilo and litre stock, recipes in grams and millilitres, float precision 5: exact end to end.

Ingredients are bought by the kilo and the litre at odd rates, recipes are written in grams and
millilitres (small quantities: 7 g, 0.5 g), a prepared ingredient is made from its BOM and a dish is
sold. Quantities must be exact (no rounding of 0.0005 kg), and the recipe cost, the BOM cost, the stock
ledger and the cost of goods sold must agree with qty x rate, each posting to the paisa.

bench --site test.resto run-tests --module resto.tests.test_units_exact
"""

import json

import frappe
from erpnext.stock.get_item_details import get_conversion_factor
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from resto.resto import floor
from resto.tests import fixtures as fx

PRECISION = 5
# One posting is rounded to the currency (2 decimals): it is within half a paisa of qty x rate.
HALF_PAISA = 0.005 + 1e-9

SPICE_RATE = 35.37  # per Kg
OIL_RATE = 81.13  # per Litre
DISH_PRICE = 50


def _set_float_precision(value):
	frappe.db.set_single_value("System Settings", "float_precision", value)
	frappe.db.set_default("float_precision", value)
	frappe.clear_cache()
	if hasattr(frappe.local, "system_settings"):
		del frappe.local.system_settings


def _unit(name):
	if not frappe.db.exists("UOM", name):
		fx.doc("UOM", name, uom_name=name)
	frappe.db.set_value("UOM", name, "must_be_whole_number", 0)


def _sle(voucher_type, voucher_no, item_code=None):
	cond, values = "", [voucher_type, voucher_no]
	if item_code:
		cond, values = " and item_code = %s", values + [item_code]
	return frappe.db.sql(
		f"""select ifnull(sum(stock_value_difference), 0), ifnull(sum(actual_qty), 0) from `tabStock Ledger Entry`
		where voucher_type = %s and voucher_no = %s and is_cancelled = 0{cond}""",
		values,
	)[0]


def _gl(voucher_type, voucher_no):
	out = {}
	for r in frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		out[r.account] = out.get(r.account, 0) + flt(r.debit) - flt(r.credit)
	return out


def _bin(item_code, field="actual_qty"):
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": fx.KITCHEN}, field))


class TestUnitsExact(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.saved_precision = frappe.db.get_single_value("System Settings", "float_precision")
		_set_float_precision(PRECISION)

	@classmethod
	def tearDownClass(cls):
		_set_float_precision(cls.saved_precision or "")
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")
		for unit in ("Kg", "Gram", "Litre", "Millilitre"):
			_unit(unit)
		tag = frappe.generate_hash(length=6).upper()
		self.spice = self.item(f"RUX-SPICE-{tag}", "Kg", "Gram")
		self.oil = self.item(f"RUX-OIL-{tag}", "Litre", "Millilitre")
		self.sauce = self.item(f"RUX-SAUCE-{tag}", "Kg", "Gram", group="Resto Prepared")
		self.dish = fx.item(f"RUX-DISH-{tag}", stock=0, group="Resto Food").name
		fx.price(self.dish, DISH_PRICE)

	def item(self, code, stock_uom, small_uom, group="Resto Raw"):
		"""A stock item by the kilo or litre whose recipes use grams or millilitres. The site's own unit
		conversion is used; an item row is added only where the site has none."""
		code = fx.item(code, stock_uom, group=group).name
		factor = flt((get_conversion_factor(code, small_uom) or {}).get("conversion_factor"))
		if not factor:
			item = frappe.get_doc("Item", code)
			item.append("uoms", {"uom": small_uom, "conversion_factor": 0.001})
			item.save(ignore_permissions=True)
			factor = flt((get_conversion_factor(code, small_uom) or {}).get("conversion_factor"))
		self.assertEqual(factor, 0.001)
		return code

	def recipe(self, recipe_type, item, rows, quantity=1):
		doc = frappe.get_doc(
			{
				"doctype": "Resto Recipe",
				"recipe_type": recipe_type,
				"item": item,
				"company": fx.COMPANY,
				"quantity": quantity,
				"items": [{"item_code": c, "qty": q, "uom": u} for c, q, u in rows],
			}
		)
		doc.insert(ignore_permissions=True)
		return frappe.get_doc("Resto Recipe", doc.name)

	def assertPaisa(self, posted, exact, msg=None):
		self.assertLessEqual(abs(flt(posted) - exact), HALF_PAISA, msg or f"{posted} is not {exact} to the paisa")

	def test_kilo_stock_gram_recipes_are_exact_end_to_end(self):
		self.assertEqual(frappe.get_precision("Bin", "actual_qty"), PRECISION)

		# Bought by the kilo and the litre at odd rates.
		receipt = fx.receive(fx.KITCHEN, [(self.spice, 1, SPICE_RATE), (self.oil, 1, OIL_RATE)])
		self.assertEqual(flt(_sle("Stock Entry", receipt.name, self.spice)[0]), SPICE_RATE)
		self.assertEqual(flt(_sle("Stock Entry", receipt.name, self.oil)[0]), OIL_RATE)

		# Prepared sauce, 1 Kg from 7 g of spice and 13 ml of oil.
		sauce = self.recipe("Prepared Ingredient", self.sauce, [(self.spice, 7, "Gram"), (self.oil, 13, "Millilitre")])
		rows = {r.item_code: r for r in sauce.items}
		self.assertEqual(flt(rows[self.spice].stock_qty, 9), 0.007)
		self.assertEqual(flt(rows[self.oil].stock_qty, 9), 0.013)
		sauce_cost = 0.007 * SPICE_RATE + 0.013 * OIL_RATE  # 1.30228
		self.assertAlmostEqual(flt(sauce.total_cost), sauce_cost, 6)

		# Its BOM: quantities in kilos and litres, exact; each amount within half a paisa.
		bom = frappe.get_doc("BOM", sauce.bom)
		bom_rows = {r.item_code: r for r in bom.items}
		self.assertEqual(flt(bom_rows[self.spice].stock_qty, 9), 0.007)
		self.assertEqual(flt(bom_rows[self.oil].stock_qty, 9), 0.013)
		self.assertEqual(flt(bom_rows[self.spice].rate), SPICE_RATE)
		self.assertEqual(flt(bom_rows[self.oil].rate), OIL_RATE)
		self.assertPaisa(bom_rows[self.spice].amount, 0.007 * SPICE_RATE)
		self.assertPaisa(bom_rows[self.oil].amount, 0.013 * OIL_RATE)
		self.assertLessEqual(abs(flt(bom.raw_material_cost) - sauce_cost), 2 * HALF_PAISA)

		# Made: exactly 7 g and 13 ml leave the kitchen, and the value moves without loss.
		prep = frappe.new_doc("Resto Prep")
		prep.company = fx.COMPANY
		prep.warehouse = fx.KITCHEN
		prep.append("items", {"recipe": sauce.name, "qty": 1})
		prep.insert(ignore_permissions=True)
		prep.submit()
		se = frappe.db.get_value("Resto Prep Item", {"parent": prep.name}, "stock_entry")
		self.assertAlmostEqual(_bin(self.spice), 1 - 0.007, 9)
		self.assertAlmostEqual(_bin(self.oil), 1 - 0.013, 9)
		self.assertAlmostEqual(_bin(self.sauce), 1, 9)
		spice_out, spice_qty = _sle("Stock Entry", se, self.spice)
		oil_out, oil_qty = _sle("Stock Entry", se, self.oil)
		self.assertAlmostEqual(flt(spice_qty), -0.007, 9)
		self.assertAlmostEqual(flt(oil_qty), -0.013, 9)
		self.assertPaisa(-flt(spice_out), 0.007 * SPICE_RATE)
		self.assertPaisa(-flt(oil_out), 0.013 * OIL_RATE)
		sauce_in = flt(_sle("Stock Entry", se, self.sauce)[0])
		self.assertAlmostEqual(sauce_in, -(flt(spice_out) + flt(oil_out)), 2)
		self.assertAlmostEqual(flt(_sle("Stock Entry", se)[0]), 0, 2)
		sauce_rate = _bin(self.sauce, "valuation_rate")
		self.assertPaisa(sauce_rate, sauce_in)

		# A dish of 30 g of sauce and 0.5 g of spice, in grams on the recipe.
		dish = self.recipe("Menu Dish", self.dish, [(self.sauce, 30, "Gram"), (self.spice, 0.5, "Gram")])
		dish_rows = {r.item_code: r for r in dish.items}
		self.assertEqual(flt(dish_rows[self.spice].stock_qty, 9), 0.0005)
		# The recipe costs a prepared ingredient by its own recipe; the stock moves at its valuation.
		dish_cost = 0.03 * sauce_cost + 0.0005 * SPICE_RATE
		self.assertAlmostEqual(flt(dish.total_cost), dish_cost, 6)
		self.assertEqual(flt(dish.selling_rate), DISH_PRICE)
		self.assertEqual(flt(dish.food_cost_percent), flt(dish_cost / DISH_PRICE * 100, 2))
		bundle = {r.item_code: flt(r.qty, 9) for r in frappe.get_doc("Product Bundle", self.dish).items}
		self.assertEqual(bundle, {self.sauce: 0.03, self.spice: 0.0005})

		# Three sold: exactly 1.5 g of spice and 90 g of sauce leave stock, at cost to the paisa, and the
		# cost of goods sold is the stock value that left.
		order = floor.create_order(order_type="Takeaway", customer_name="Units Guest")
		floor.add_items(order, json.dumps([{"item_code": self.dish, "qty": 3}]))
		floor.send_to_kitchen(order)
		bill = floor.get_bill(order)
		si = floor.settle(order, json.dumps([{"mode_of_payment": "Cash", "amount": bill["payable"]}]))["sales_invoice"]
		self.assertAlmostEqual(_bin(self.spice), 1 - 0.007 - 0.0015, 9)
		self.assertAlmostEqual(_bin(self.sauce), 1 - 0.09, 9)
		spice_sold, spice_qty = _sle("Sales Invoice", si, self.spice)
		sauce_sold, sauce_qty = _sle("Sales Invoice", si, self.sauce)
		self.assertAlmostEqual(flt(spice_qty), -0.0015, 9)
		self.assertAlmostEqual(flt(sauce_qty), -0.09, 9)
		self.assertPaisa(-flt(spice_sold), 0.0015 * SPICE_RATE)
		self.assertPaisa(-flt(sauce_sold), 0.09 * sauce_rate)
		stock_out = -flt(_sle("Sales Invoice", si)[0])
		self.assertLessEqual(abs(stock_out - 3 * dish_cost), 2 * HALF_PAISA)

		gl = _gl("Sales Invoice", si)
		self.assertAlmostEqual(sum(gl.values()), 0, 2)
		cogs = sum(v for a, v in gl.items() if frappe.get_cached_value("Account", a, "root_type") == "Expense")
		self.assertAlmostEqual(cogs, stock_out, 2)
		stock_accounts = [a for a in gl if frappe.get_cached_value("Account", a, "account_type") == "Stock"]
		self.assertAlmostEqual(-sum(gl[a] for a in stock_accounts), stock_out, 2)
