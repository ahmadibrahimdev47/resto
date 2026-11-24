"""End-to-end tests of Resto restaurant operations. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_restaurant_flow
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from resto.resto import billing, floor
from resto.tests import fixtures as fx


def lines(order):
	return frappe.get_doc("Resto Order", order).items


def gl_by_account(voucher_type, voucher_no):
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	)
	out = {}
	for r in rows:
		out[r.account] = out.get(r.account, 0) + flt(r.debit) - flt(r.credit)
	return out


def sle_value(voucher_type, voucher_no):
	return flt(
		frappe.db.sql(
			"select sum(stock_value_difference) from `tabStock Ledger Entry` where voucher_type=%s and voucher_no=%s and is_cancelled=0",
			(voucher_type, voucher_no),
		)[0][0]
	)


class TestRestaurantFlow(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		cls.service = frappe.db.get_single_value("Resto Settings", "service_charge_account")
		cls.vat = f"VAT 14 - {fx.ABBR}"

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 0)
		frappe.db.set_single_value("Resto Settings", "vat_on_service", 1)
		if "pos_next" in frappe.get_installed_apps():
			# Something on submit commits; never let a tax inclusive test setting leak into other tests.
			frappe.db.delete("POS Settings", {"pos_profile": fx.PROFILE, "tax_inclusive": 1})
		frappe.db.set_value("POS Profile", fx.PROFILE, "taxes_and_charges", f"Resto VAT - {fx.ABBR}")
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.clear_document_cache("POS Profile", fx.PROFILE)
		# Free all tables between tests.
		for name in frappe.get_all("Resto Order", filters={"status": ("in", ("Open", "Bill Requested"))}, pluck="name"):
			frappe.db.set_value("Resto Order", name, "status", "Cancelled")

	def tearDown(self):
		frappe.set_user("Administrator")

	def new_dine_in(self, table="T1", guests=2):
		return floor.create_order(order_type="Dine In", table=table, guests=guests)

	def burger(self, qty=1, extra=False):
		mods = [{"group": "Doneness", "option": "Medium"}]
		if extra:
			mods.append({"group": "Extras", "option": "Extra Cheese"})
		return {"item_code": "DISH-BURGER", "qty": qty, "modifiers": mods}

	def pay_all(self, order, cash=None, lines_=None):
		bill = floor.get_bill(order, json.dumps(lines_) if lines_ else None)
		return floor.settle(
			order,
			json.dumps([{"mode_of_payment": "Cash", "amount": cash if cash is not None else bill["payable"]}]),
			json.dumps(lines_) if lines_ else None,
		), bill

	# ------------------------------------------------------------------ dine in

	def test_dine_in_bill_stock_and_accounts(self):
		order = self.new_dine_in("T1")
		floor.add_items(
			order,
			json.dumps(
				[self.burger(2, extra=True), {"item_code": "DISH-FRIES", "qty": 1}, {"item_code": "DRK-COLA", "qty": 2}]
			),
		)
		doc = frappe.get_doc("Resto Order", order)
		self.assertEqual(doc.net_total, 2 * (150 + 15) + 40 + 2 * 25)  # 420
		self.assertEqual(doc.service_charge_percent, 12)

		tickets = floor.send_to_kitchen(order)
		self.assertEqual(len(tickets), 2)
		stations = {frappe.db.get_value("Resto Kitchen Ticket", t, "station") for t in tickets}
		self.assertEqual(stations, {"Grill", "Bar"})
		self.assertTrue(all(d.kitchen_status == "Sent" for d in lines(order)))

		# Kitchen flow updates the order lines.
		grill = next(t for t in tickets if frappe.db.get_value("Resto Kitchen Ticket", t, "station") == "Grill")
		frappe.get_attr("resto.resto.kitchen.set_ticket_status")(grill, "Ready")
		self.assertIn("Ready", [d.kitchen_status for d in lines(order)])

		preview = floor.request_bill(order)
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Bill Requested")
		self.assertAlmostEqual(preview["net_total"], 420, 2)
		self.assertAlmostEqual(preview["service"], 50.4, 2)
		# VAT is charged on items plus service: 14% of 470.40.
		self.assertAlmostEqual(preview["tax_total"], 65.86, 2)
		self.assertAlmostEqual(preview["grand_total"], 536.26, 2)

		bun_before = fx.bin_qty("RAW-BUN")
		cola_before = fx.bin_qty("DRK-COLA")
		result, bill = self.pay_all(order, cash=600)
		si = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(si.is_pos, 1)
		self.assertEqual(si.resto_order, order)
		self.assertEqual(si.resto_table, "T1")
		self.assertAlmostEqual(si.grand_total, 536.26, 2)
		self.assertAlmostEqual(si.change_amount, 600 - bill["payable"], 2)
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")

		# Extra cheese is its own invoice line at its own price.
		cheese = [d for d in si.items if d.item_code == "EXTRA-CHEESE"]
		self.assertEqual(len(cheese), 1)
		self.assertEqual((cheese[0].qty, cheese[0].rate), (2, 15))

		# Stock: bundles deduct components; cola is a stock item sold directly.
		self.assertAlmostEqual(bun_before - fx.bin_qty("RAW-BUN"), 2, 6)
		self.assertAlmostEqual(cola_before - fx.bin_qty("DRK-COLA"), 2, 6)

		# Accounts: balanced; service and VAT posted to their accounts; income = net total.
		gl = gl_by_account("Sales Invoice", si.name)
		self.assertAlmostEqual(sum(gl.values()), 0, 2)
		self.assertAlmostEqual(-gl[self.service], 50.4, 2)
		self.assertAlmostEqual(-gl[self.vat], 65.86, 2)
		income = sum(-v for a, v in gl.items() if frappe.db.get_value("Account", a, "root_type") == "Income")
		self.assertAlmostEqual(income, 420, 2)
		# Cost of goods sold equals the stock value that left the warehouse.
		stock_account = frappe.db.get_value("Warehouse", fx.KITCHEN, "account") or frappe.get_cached_value(
			"Company", fx.COMPANY, "default_inventory_account"
		)
		self.assertAlmostEqual(gl.get(stock_account, 0), sle_value("Sales Invoice", si.name), 2)

		# The table is free again.
		floor_data = floor.get_floor()
		t1 = next(t for h in floor_data["halls"] for t in h["tables"] if t["name"] == "T1")
		self.assertEqual(t1["status"], "free")

	def test_vat_on_service_from_template(self):
		frappe.db.set_value("POS Profile", fx.PROFILE, "taxes_and_charges", f"Resto VAT on Service - {fx.ABBR}")
		frappe.clear_document_cache("POS Profile", fx.PROFILE)
		order = self.new_dine_in("T2")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 5}]))  # 200
		floor.send_to_kitchen(order)
		bill = floor.get_bill(order)
		self.assertAlmostEqual(bill["service"], 24, 2)
		self.assertAlmostEqual(bill["tax_total"], 0.14 * 224, 2)  # VAT on food + service
		result, _ = self.pay_all(order)
		gl = gl_by_account("Sales Invoice", result["sales_invoice"])
		self.assertAlmostEqual(-gl[self.vat], 31.36, 2)
		self.assertAlmostEqual(-gl[self.service], 24, 2)

	def test_split_by_lines_with_discount_adds_up(self):
		order = self.new_dine_in("T3", guests=3)
		floor.add_items(order, json.dumps([self.burger(3), {"item_code": "DRK-COLA", "qty": 3}]))
		floor.send_to_kitchen(order)
		floor.apply_discount(order, percentage=10, reason="Regular guest")
		doc = frappe.get_doc("Resto Order", order)
		self.assertAlmostEqual(doc.discount_amount, 52.5, 2)  # 10% of 525

		burger_line = next(d for d in doc.items if d.item_code == "DISH-BURGER")
		cola_line = next(d for d in doc.items if d.item_code == "DRK-COLA")
		first, _ = self.pay_all(order, lines_={burger_line.name: 1, cola_line.name: 1})
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Open")
		second, _ = self.pay_all(order)
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")

		invoices = [frappe.get_doc("Sales Invoice", r["sales_invoice"]) for r in (first, second)]
		self.assertAlmostEqual(sum(si.discount_amount for si in invoices), 52.5, 2)
		self.assertAlmostEqual(sum(si.net_total for si in invoices), 525 - 52.5, 2)
		self.assertEqual(sum(d.qty for si in invoices for d in si.items if d.item_code == "DISH-BURGER"), 3)
		self.assertTrue(all(d.billed_qty == d.qty for d in lines(order)))

	def test_cancelled_invoice_reopens_order(self):
		order = self.new_dine_in("T4")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
		floor.send_to_kitchen(order)
		result, _ = self.pay_all(order)
		si = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		si.cancel()
		doc = frappe.get_doc("Resto Order", order)
		self.assertEqual(doc.status, "Open")
		self.assertEqual(doc.items[0].billed_qty, 0)
		self.assertTrue(doc.invoices[0].cancelled)
		result2, _ = self.pay_all(order)
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")
		self.assertNotEqual(result2["sales_invoice"], si.name)

	# ------------------------------------------------------------------ controls

	def test_sent_lines_are_locked_and_void_needs_manager(self):
		order = self.new_dine_in("T5")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
		floor.send_to_kitchen(order)
		line = lines(order)[0]

		doc = frappe.get_doc("Resto Order", order)
		doc.items[0].qty = 1
		self.assertRaises(frappe.ValidationError, doc.save)

		self.assertRaises(frappe.ValidationError, floor.update_line, order, line.name, 1)

		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.PermissionError, floor.void_line, order, line.name, "Guest changed mind")
		frappe.set_user("Administrator")
		floor.void_line(order, line.name, "Guest changed mind")
		doc = frappe.get_doc("Resto Order", order)
		self.assertEqual(doc.items[0].kitchen_status, "Void")
		self.assertEqual(doc.net_total, 0)
		self.assertTrue(
			frappe.db.exists("Resto Kitchen Ticket", {"resto_order": order, "ticket_type": "Void"})
		)
		self.assertRaises(frappe.ValidationError, floor.get_bill, order)

	def test_server_ignores_client_prices_and_status(self):
		order = self.new_dine_in("T6")
		doc = frappe.get_doc("Resto Order", order)
		doc.append("items", {"item_code": "DISH-FRIES", "qty": 1, "rate": 1, "kitchen_status": "Served", "billed_qty": 1})
		doc.save()
		row = frappe.get_doc("Resto Order", order).items[0]
		self.assertEqual((row.rate, row.kitchen_status, row.billed_qty), (40, "Not Sent", 0))

	def test_discount_limits(self):
		order = self.new_dine_in("T1")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 10}]))
		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.PermissionError, floor.apply_discount, order, 5, 0, "x")
		frappe.set_user("cashier@resto.test")
		floor.apply_discount(order, 5, 0, "loyal")
		self.assertRaises(frappe.PermissionError, floor.apply_discount, order, 15, 0, "too much")
		frappe.set_user("Administrator")
		floor.apply_discount(order, 50, 0, "manager")

	def test_move_and_merge(self):
		a = self.new_dine_in("T1")
		b = self.new_dine_in("T2")
		floor.add_items(a, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.add_items(b, json.dumps([{"item_code": "DRK-COLA", "qty": 2}]))
		floor.send_to_kitchen(b)
		floor.move_table(b, "T3")
		self.assertEqual(frappe.db.get_value("Resto Order", b, "resto_table"), "T3")
		self.assertRaises(frappe.ValidationError, floor.move_table, b, "T1")  # T1 is busy

		floor.merge_orders(a, b)
		self.assertEqual(frappe.db.get_value("Resto Order", b, "status"), "Merged")
		merged = frappe.get_doc("Resto Order", a)
		self.assertEqual(len(merged.items), 2)
		self.assertEqual(merged.net_total, 40 + 50)
		self.assertEqual(merged.guests, 4)
		self.assertEqual(
			frappe.db.get_value("Resto Kitchen Ticket", {"ticket_type": "Order", "station": "Bar", "resto_order": a}, "resto_table"),
			"T1",
		)

	def test_hold_and_fire_course(self):
		order = self.new_dine_in("T4")
		floor.add_items(
			order,
			json.dumps(
				[{"item_code": "DISH-FRIES", "qty": 1, "course": "Starter"}, dict(self.burger(1), course="Main Course", hold=1)]
			),
		)
		floor.send_to_kitchen(order)
		status = {d.item_code: d.kitchen_status for d in lines(order)}
		self.assertEqual(status, {"DISH-FRIES": "Sent", "DISH-BURGER": "Not Sent"})
		floor.fire_course(order, "Main Course")
		self.assertTrue(all(d.kitchen_status == "Sent" for d in lines(order)))

	def test_modifier_rules(self):
		order = self.new_dine_in("T5")
		self.assertRaises(
			frappe.ValidationError, floor.add_items, order, json.dumps([{"item_code": "DISH-BURGER", "qty": 1}])
		)
		bad = {"item_code": "DISH-BURGER", "qty": 1, "modifiers": [{"group": "Doneness", "option": "Medium"}, {"group": "Doneness", "option": "Well Done"}]}
		self.assertRaises(frappe.ValidationError, floor.add_items, order, json.dumps([bad]))

	# ------------------------------------------------------------------ takeaway / delivery / shift

	def test_delivery_with_fee_paid_before_cooking(self):
		order = floor.create_order(order_type="Delivery", customer_name="Mona", phone="0100", address="Street 1")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
		result, bill = self.pay_all(order)
		si = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertIn("DELIVERY-FEE", [d.item_code for d in si.items])
		self.assertFalse([t for t in si.taxes if t.account_head == self.service and t.tax_amount])
		self.assertAlmostEqual(si.net_total, 80 + 20, 2)
		self.assertTrue(frappe.db.exists("Resto Kitchen Ticket", {"resto_order": order}))

	def test_posnext_shift_required(self):
		if "pos_next" not in frappe.get_installed_apps():
			return
		frappe.db.set_single_value("Resto Settings", "require_pos_shift", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		order = self.new_dine_in("T6")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		self.assertRaises(frappe.ValidationError, self.pay_all, order)

		shift = frappe.get_doc(
			{
				"doctype": "POS Opening Shift",
				"period_start_date": frappe.utils.now_datetime(),
				"company": fx.COMPANY,
				"pos_profile": fx.PROFILE,
				"user": "Administrator",
				"balance_details": [{"mode_of_payment": "Cash", "amount": 0}],
			}
		)
		shift.insert(ignore_permissions=True)
		shift.submit()
		result, _ = self.pay_all(order)
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["sales_invoice"], "posa_pos_opening_shift"), shift.name)

	# ------------------------------------------------------------------ prep and waste

	def test_prep_makes_prepared_stock_from_bom(self):
		patty_before = fx.bin_qty("PREP-PATTY")
		beef_before = fx.bin_qty("RAW-BEEF")
		prep = frappe.new_doc("Resto Prep")
		prep.company = fx.COMPANY
		prep.get_suggestions()
		row = next(d for d in prep.items if d.item_code == "PREP-PATTY")
		self.assertAlmostEqual(row.suggested_qty, max(20 - patty_before, 0), 6)
		for d in prep.items:
			d.qty = 10 if d.item_code == "PREP-PATTY" else 0
		prep.insert()
		prep.submit()
		self.assertAlmostEqual(fx.bin_qty("PREP-PATTY") - patty_before, 10, 6)
		self.assertAlmostEqual(beef_before - fx.bin_qty("RAW-BEEF"), 1.5, 6)
		se = prep.items[0].stock_entry
		# Manufacture moves value from raw to prepared; nothing is created or lost.
		self.assertAlmostEqual(sle_value("Stock Entry", se), 0, 2)
		prep.cancel()
		self.assertAlmostEqual(fx.bin_qty("PREP-PATTY"), patty_before, 6)

	def test_waste_of_a_dish_issues_components_at_cost(self):
		bun_before = fx.bin_qty("RAW-BUN")
		waste = frappe.new_doc("Resto Waste")
		waste.company = fx.COMPANY
		waste.append("items", {"item_code": "DISH-BURGER", "qty": 2, "reason": "Dropped"})
		waste.append("items", {"item_code": "RAW-POTATO", "qty": 1.5, "reason": "Spoiled"})
		waste.insert()
		waste.submit()
		waste.reload()
		self.assertAlmostEqual(bun_before - fx.bin_qty("RAW-BUN"), 2, 6)
		posted = -sle_value("Stock Entry", waste.stock_entry)
		self.assertAlmostEqual(waste.total_amount, posted, 2)
		gl = gl_by_account("Stock Entry", waste.stock_entry)
		self.assertAlmostEqual(gl[waste.waste_account], posted, 2)
		waste.cancel()
		self.assertAlmostEqual(fx.bin_qty("RAW-BUN"), bun_before, 6)

	# ------------------------------------------------------------------ reports

	def test_reports_run_and_food_cost_matches_ledger(self):
		order = self.new_dine_in("T2")
		floor.add_items(order, json.dumps([self.burger(1), {"item_code": "DRK-COLA", "qty": 1}]))
		floor.send_to_kitchen(order)
		result, _ = self.pay_all(order)
		today = frappe.utils.nowdate()
		base = {"company": fx.COMPANY, "from_date": today, "to_date": today}
		from frappe.desk.query_report import run

		for name, extra in (
			("Resto Sales Analysis", [{"group_by": g} for g in ("Day", "Hour", "Weekday", "Order Type", "Table", "Hall", "Waiter", "Item", "Item Group", "Payment Method")]),
			("Resto Menu Engineering", [{}]),
			("Resto Food Cost", [{"group_by": g} for g in ("Item", "Item Group", "Day")]),
			("Resto Kitchen Times", [{"group_by": g} for g in ("Station", "Item", "Day", "Hour", "Waiter")]),
			("Resto Table Analysis", [{"group_by": g} for g in ("Table", "Hall", "Waiter", "Weekday", "Hour")]),
			("Resto Voids and Discounts", [{"type": t} for t in ("Voided Items", "Discounts", "Cancelled Orders", "Cancelled Invoices")]),
			("Resto Kitchen vs Invoices", [{"show": "All Lines"}, {"show": "Exceptions"}, {"station": "Grill"}]),
			("Resto Waste Analysis", [{"group_by": g} for g in ("Item", "Item Group", "Reason", "Day")]),
			("Resto Customer Insights", [{}]),
			("Resto Purchase Price Changes", [{}]),
			("Resto Negative Prepared Stock", [{}]),
		):
			for f in extra:
				run(name, filters={**base, **f}, ignore_prepared_report=True)

		fc = run("Resto Food Cost", filters={**base, "group_by": "Item"}, ignore_prepared_report=True)
		posted = sum(flt(r.get("actual_cost")) for r in fc["result"] if isinstance(r, dict))
		ledger = -flt(
			frappe.db.sql(
				"""select sum(stock_value_difference) from `tabStock Ledger Entry` where voucher_type='Sales Invoice'
				and is_cancelled=0 and posting_date=%s and company=%s""",
				(today, fx.COMPANY),
			)[0][0]
		)
		self.assertAlmostEqual(posted, ledger, 2)

		from resto.resto.analytics import get_dashboard

		dash = get_dashboard(today, today, fx.COMPANY)
		self.assertTrue(dash["kpis"]["net_sales"] > 0)

	def test_setup_check(self):
		from resto.resto import setup_check

		results = setup_check.run()
		errors = [r["message"] for r in results if r["level"] == "error"]
		self.assertEqual(errors, [], errors)
		# A POS Profile that does not update stock is a hard error: recipes would never be deducted.
		frappe.db.set_value("POS Profile", fx.PROFILE, "update_stock", 0)
		try:
			errors = [r["message"] for r in setup_check.run() if r["level"] == "error"]
			self.assertTrue(any("update stock" in e for e in errors), errors)
		finally:
			frappe.db.set_value("POS Profile", fx.PROFILE, "update_stock", 1)
		frappe.set_user("waiter@resto.test")
		self.assertRaises(frappe.PermissionError, setup_check.run)

	def test_setup_check_leaves_out_variant_templates(self):
		from resto.resto import setup_check

		# A template is never sold, so no price or recipe is asked of it; a plain item is.
		fx.item("TPL-GRILL-MIX", stock=0, group="Resto Menu")
		frappe.db.set_value("Item", "TPL-GRILL-MIX", {"disabled": 0, "has_variants": 0})  # a run before left it off
		said = lambda: " ".join(r["message"] for r in setup_check.run())
		self.assertIn("TPL-GRILL-MIX", said())
		frappe.db.set_value("Item", "TPL-GRILL-MIX", "has_variants", 1)
		try:
			self.assertNotIn("TPL-GRILL-MIX", said())
		finally:
			frappe.db.set_value("Item", "TPL-GRILL-MIX", "disabled", 1)

	# ------------------------------------------------------------------ review fixes

	def test_tax_inclusive_follows_posnext(self):
		if "pos_next" not in frappe.get_installed_apps() or not frappe.db.has_column("POS Settings", "tax_inclusive"):
			return
		ps = frappe.get_doc({"doctype": "POS Settings", "pos_profile": fx.PROFILE, "tax_inclusive": 1})
		ps.flags.ignore_mandatory = True
		ps.insert(ignore_permissions=True)
		# Takeaway, no service: the menu price is what the guest pays, VAT inside it.
		order = floor.create_order(order_type="Takeaway", customer_name="Ali")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))  # 80
		bill = floor.get_bill(order)
		self.assertAlmostEqual(bill["payable"], 80, 2)
		result, _ = self.pay_all(order)
		si = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertAlmostEqual(si.grand_total, 80, 2)
		self.assertAlmostEqual(si.change_amount, 0, 2)
		# Dine in has a service charge: refused rather than billed wrong.
		order = self.new_dine_in("T1")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		self.assertRaises(frappe.ValidationError, floor.get_bill, order)

	def test_void_after_cooking_posts_waste(self):
		order = self.new_dine_in("T2")
		floor.add_items(order, json.dumps([self.burger(1, extra=True)]))
		floor.send_to_kitchen(order)
		bun = fx.bin_qty("RAW-BUN")
		cheese = fx.bin_qty("RAW-CHEESE")
		floor.void_line(order, lines(order)[0].name, "Too salty", cooked=1)
		waste = frappe.get_all("Resto Waste", filters={"remarks": ("like", f"%{order}%"), "docstatus": 1}, pluck="name")
		self.assertEqual(len(waste), 1)
		self.assertAlmostEqual(bun - fx.bin_qty("RAW-BUN"), 1, 6)
		self.assertAlmostEqual(cheese - fx.bin_qty("RAW-CHEESE"), 0.02 + 0.03, 6)  # dish + extra cheese

	def test_part_paid_rest_voided_closes_order(self):
		order = self.new_dine_in("T3")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}, {"item_code": "DRK-COLA", "qty": 1}]))
		floor.send_to_kitchen(order)
		fries, cola = lines(order)
		self.pay_all(order, lines_={fries.name: 1})
		floor.void_line(order, cola.name, "Not wanted")
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")

	def test_discount_after_part_paid_counts_only_unbilled(self):
		order = self.new_dine_in("T4")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 10}]))  # 400
		floor.send_to_kitchen(order)
		self.pay_all(order, lines_={lines(order)[0].name: 9})
		frappe.set_user("cashier@resto.test")
		# 10% of the whole order is 40, but it would all go on the last 40: 100%.
		self.assertRaises(frappe.PermissionError, floor.apply_discount, order, 10, 0, "x")
		floor.apply_discount(order, 0, 4, "ok")  # 10% of what is left

	def test_printed_bill_reopens_on_any_change(self):
		order = self.new_dine_in("T5")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
		floor.send_to_kitchen(order)
		floor.request_bill(order)
		doc = frappe.get_doc("Resto Order", order)
		doc.discount_amount = 8
		doc.discount_reason = "x"
		doc.save()
		doc.reload()
		self.assertEqual(doc.status, "Open")
		self.assertFalse(doc.bill_details)

	def test_bill_guards(self):
		order = self.new_dine_in("T6")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 2}]))
		floor.send_to_kitchen(order)
		bill = floor.get_bill(order)
		pay = json.dumps([{"mode_of_payment": "Cash", "amount": 500}])
		# The amount shown must still be the bill.
		self.assertRaises(frappe.ValidationError, floor.settle, order, pay, None, bill["payable"] + 10)
		first = floor.settle(order, pay, None, bill["payable"], "req-1")
		# A retried click returns the same invoice.
		again = billing.settle(frappe.get_doc("Resto Order", order), [{"mode_of_payment": "Cash", "amount": 500}], None, None, "req-1")
		self.assertEqual(again.name, first["sales_invoice"])
		# The same retry through the floor API, after the payment closed the order.
		self.assertEqual(frappe.db.get_value("Resto Order", order, "status"), "Paid")
		self.assertEqual(floor.settle(order, pay, None, bill["payable"], "req-1")["sales_invoice"], first["sales_invoice"])
		self.assertRaises(frappe.ValidationError, floor.settle, order, pay, None, None, "req-other")
		# Kitchen ticket and pre-bill print formats render (right-to-left in Arabic).
		ticket = frappe.get_all("Resto Kitchen Ticket", filters={"resto_order": order}, pluck="name", limit=1)[0]
		html = frappe.get_print("Resto Kitchen Ticket", ticket, "Resto Kitchen Ticket", no_letterhead=1)
		self.assertIn("Dish Fries", html)
		html = frappe.get_print("Resto Order", order, "Resto Pre-Bill", no_letterhead=1)
		self.assertIn(order, html)
		si = frappe.get_doc("Sales Invoice", first["sales_invoice"])
		# Only a manager cancels a floor bill.
		frappe.set_user("cashier@resto.test")
		self.assertRaises(frappe.PermissionError, billing.before_sales_invoice_cancel, si)
		frappe.set_user("Administrator")
		# New guests at the table, then the old bill is cancelled: the old order is listed apart.
		new = self.new_dine_in("T6")
		si.cancel()
		self.assertEqual(frappe.db.get_value("Resto Order", order, "rebill"), 1)
		floor.add_items(new, json.dumps([{"item_code": "DRK-COLA", "qty": 1}]))
		data = floor.get_floor()
		self.assertIn(order, [o.name for o in data["orders"]])
		t6 = next(t for h in data["halls"] for t in h["tables"] if t["name"] == "T6")
		self.assertEqual(t6["order"]["name"], new)
		# An amended copy of a floor bill is refused.
		copy = frappe.copy_doc(si)
		copy.amended_from = si.name
		copy.resto_order = order
		self.assertRaises(frappe.ValidationError, copy.insert)

	def test_closing_shift_must_list_floor_bills(self):
		if not frappe.get_meta("Sales Invoice").has_field("posa_pos_opening_shift"):
			return
		order = self.new_dine_in("T1")
		floor.add_items(order, json.dumps([{"item_code": "DISH-FRIES", "qty": 1}]))
		floor.send_to_kitchen(order)
		result, _ = self.pay_all(order)
		frappe.db.set_value("Sales Invoice", result["sales_invoice"], "posa_pos_opening_shift", "TEST-SHIFT")
		closing = frappe._dict(pos_opening_shift="TEST-SHIFT", pos_transactions=[])
		self.assertRaises(frappe.ValidationError, billing.validate_closing_shift, closing)
		closing.pos_transactions = [frappe._dict(sales_invoice=result["sales_invoice"])]
		billing.validate_closing_shift(closing)

	def test_reports_leave_out_opening_stock(self):
		from resto.resto import report_utils

		today = frappe.utils.nowdate()
		f = frappe._dict(company=fx.COMPANY, from_date=today, to_date=today)
		before = report_utils.count_differences(f)
		fx.item("RAW-SALT", "Kg", group="Resto Raw", valuation=5)
		sr = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": fx.COMPANY,
				"purpose": "Opening Stock",
				"expense_account": frappe.db.get_value("Account", {"company": fx.COMPANY, "account_name": "Temporary Opening"}),
				"items": [{"item_code": "RAW-SALT", "warehouse": fx.MAIN, "qty": 10, "valuation_rate": 5}],
			}
		)
		sr.insert(ignore_permissions=True)
		sr.submit()
		self.assertAlmostEqual(report_utils.count_differences(f), before, 2)
