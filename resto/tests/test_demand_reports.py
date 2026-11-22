"""Demand Patterns and Prep Forecast. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_demand_reports

Sales are posted at 13:30 on three Tuesdays, 17 and 24 February and 3 March 2026 (nothing else is
ever posted on those days, and 10 February stays closed). Each run uses its own items and item
group, so earlier runs do not change the numbers.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from resto.resto.doctype.resto_recipe.resto_recipe import MENU, PREPARED
from resto.resto.report.resto_demand_patterns import resto_demand_patterns as demand
from resto.resto.report.resto_prep_forecast import resto_prep_forecast as forecast
from resto.tests import fixtures as fx

TARGET = "2026-03-10"  # a Tuesday
DAYS = {"2026-03-03": (3, 2), "2026-02-24": (5, 2), "2026-02-17": (4, 2)}  # burgers, colas; 2026-02-10 closed


class TestDemandReports(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		tag = frappe.generate_hash(length=5).upper()
		cls.group = fx.doc("Item Group", f"RTF Dishes {tag}", item_group_name=f"RTF Dishes {tag}", parent_item_group="All Item Groups").name
		cls.meat = fx.item(f"RTF-MEAT-{tag}", "Kg", group="Resto Raw", valuation=300).name
		cls.bun = fx.item(f"RTF-BUN-{tag}", group="Resto Raw", valuation=5).name
		cls.patty = fx.item(f"RTF-PATTY-{tag}", "Kg", group="Resto Prepared").name
		cls.burger = fx.item(f"RTF-BURGER-{tag}", stock=0, group=cls.group).name
		cls.cola = fx.item(f"RTF-COLA-{tag}", group=cls.group, valuation=10).name
		cls.piece = fx.doc("UOM", f"RTF Piece {tag}", uom_name=f"RTF Piece {tag}", must_be_whole_number=1).name
		fx.recipe(PREPARED, cls.patty, [(cls.meat, 1.2)])  # 1 Kg patty from 1.2 Kg meat
		fx.recipe(MENU, cls.burger, [(cls.patty, 0.2), (cls.bun, 1)])
		fx.receive(fx.KITCHEN, [(cls.patty, 0.5, 400), (cls.meat, 0.1, 300)])
		fx.receive(fx.MAIN, [(cls.meat, 10, 300), (cls.bun, 2, 5)])
		for day, (burgers, colas) in DAYS.items():
			cls.invoice(day, [(cls.burger, burgers, 100), (cls.cola, colas, 20)])

	@classmethod
	def invoice(cls, day, rows):
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": fx.COMPANY,
				"customer": fx.CUSTOMER,
				"posting_date": day,
				"set_posting_time": 1,
				"posting_time": "13:30:00",
				"due_date": day,
				"update_stock": 0,
				"items": [{"item_code": c, "qty": q, "rate": r} for c, q, r in rows],
			}
		)
		si.insert(ignore_permissions=True)
		si.submit()

	def demand(self, **f):
		filters = {"company": fx.COMPANY, "from_date": "2026-02-17", "to_date": "2026-03-03", "item_group": self.group, **f}
		columns, data, *rest = demand.execute(filters)
		return {c["fieldname"]: c for c in columns}, {d["row"]: d for d in data}

	def test_weekday_average_and_total(self):
		cols, rows = self.demand(columns_by="Weekday")
		tuesday = "b_1"  # Monday is 0
		self.assertIn(tuesday, cols)
		self.assertAlmostEqual(rows[self.burger][tuesday], 4)  # (3 + 5 + 4) / 3 Tuesdays
		self.assertAlmostEqual(rows[self.burger]["b_0"], 0)
		_cols, total = self.demand(columns_by="Weekday", show_as="Total")
		self.assertAlmostEqual(total[self.burger][tuesday], 12)
		_cols, sales = self.demand(columns_by="Weekday", show_as="Total", measure="Net Sales")
		self.assertAlmostEqual(sales[self.burger][tuesday], 1200)

	def test_week_and_day_of_month(self):
		_cols, weeks = self.demand(columns_by="Week of Month", show_as="Total")
		self.assertAlmostEqual(weeks[self.burger]["b_1"], 3)  # 3 March
		self.assertAlmostEqual(weeks[self.burger]["b_3"], 4)  # 17 February
		self.assertAlmostEqual(weeks[self.burger]["b_4"], 5)  # 24 February
		_cols, days = self.demand(columns_by="Day of Month")
		self.assertAlmostEqual(days[self.burger]["b_24"], 5)

	def test_hour_average_is_per_open_day(self):
		_cols, rows = self.demand(columns_by="Hour")
		self.assertAlmostEqual(rows[self.burger]["b_13"], 4)  # 12 burgers at 13:00 over 3 open days

	def test_other_layouts_run(self):
		for columns_by in ("Month", "Hour", "Weekday and Hour"):
			cols, rows = self.demand(columns_by=columns_by)
			self.assertTrue(rows, columns_by)
		_cols, groups = self.demand(rows_by="Item Group", show_as="Total")
		self.assertAlmostEqual(groups[self.group]["b_1"], 12 + 6)
		columns, data, message = demand.execute({"company": fx.COMPANY, "from_date": "2026-02-01", "to_date": "2026-02-09", "item_group": self.group})
		self.assertEqual(data, [])
		self.assertTrue(message)

	def plan(self, **f):
		return forecast.get_plan({"company": fx.COMPANY, "target_date": TARGET, "weeks": 4, "item_group": self.group,
			"safety_margin": 10, **f})

	def test_forecast_skips_closed_days_and_adds_margin(self):
		plan = self.plan()
		self.assertEqual([str(d) for d in plan.used], ["2026-03-03", "2026-02-24", "2026-02-17"])
		dishes = {d.item: d for d in plan.dishes}
		self.assertAlmostEqual(dishes[self.burger].base, 4)
		self.assertAlmostEqual(dishes[self.burger].expected, 4.4)
		weighted = {d.item: d for d in self.plan(method="Weighted Average").dishes}
		self.assertAlmostEqual(weighted[self.burger].base, (3 * 3 + 5 * 2 + 4 * 1) / 6)
		# The page drops a 0 filter: no margin means 0, not the page default.
		plain = {d.item: d for d in self.plan(safety_margin=None).dishes}
		self.assertAlmostEqual(plain[self.burger].expected, 4)

	def test_rounding_up(self):
		self.assertEqual(forecast.round_up(2.1, self.piece), 3)
		self.assertEqual(forecast.round_up(2.0000001, self.piece), 2)
		self.assertAlmostEqual(forecast.round_up(0.3561, "Kg"), 0.357)
		self.assertEqual(forecast.round_up(-1, "Kg"), 0)

	def test_forecast_ingredients(self):
		plan = self.plan()
		prepared = {r.item: r for r in plan.prepared}
		raw = {r.item: r for r in plan.raw}
		# 4.4 burgers x 0.2 Kg patty; 0.5 Kg already in the kitchen.
		self.assertAlmostEqual(prepared[self.patty].required, 0.88)
		self.assertAlmostEqual(prepared[self.patty].to_prepare, 0.38)
		# The patty to make needs 0.38 x 1.2 Kg meat; 0.1 Kg is in the kitchen.
		self.assertAlmostEqual(raw[self.meat].required, 0.456)
		self.assertAlmostEqual(raw[self.meat].to_transfer, 0.356)
		self.assertAlmostEqual(raw[self.meat].to_buy, 0)
		# Buns: 4.4 needed, none in the kitchen, 2 in the main store.
		self.assertAlmostEqual(raw[self.bun].to_transfer, 2)
		self.assertAlmostEqual(raw[self.bun].to_buy, 2.4)
		# The cola is sold from stock as it is.
		self.assertAlmostEqual(raw[self.cola].required, 2.2)

	def test_forecast_without_history(self):
		plan = self.plan(target_date="2026-02-03")
		self.assertEqual(plan.used, [])
		self.assertTrue(plan.message)
		columns, data, message = forecast.execute({"company": fx.COMPANY, "target_date": "2026-02-03", "weeks": 2})
		self.assertEqual(data, [])

	def test_forecast_report_runs_for_the_kitchen(self):
		from frappe.desk.query_report import run

		filters = {"company": fx.COMPANY, "target_date": TARGET, "weeks": 4, "item_group": self.group}
		frappe.set_user("kitchen@resto.test")
		try:
			for view in ("Dishes", "Ingredients"):
				result = run("Resto Prep Forecast", filters={**filters, "view": view})
				self.assertTrue(result["result"], view)
		finally:
			frappe.set_user("Administrator")

	def test_forecast_drafts(self):
		filters = {"company": fx.COMPANY, "target_date": TARGET, "weeks": 4, "safety_margin": 10}
		with self.assertRaises(frappe.ValidationError):
			forecast.make_prep(frappe.as_json({**filters, "item_group": self.group}))  # a draft covers every dish
		# Drafts cover every dish, so earlier runs' items may be in them too: check this run's rows.
		tag = forecast.draft_tag(forecast.get_plan(filters))
		for doctype in ("Resto Prep", "Stock Entry"):
			for name in frappe.get_all(doctype, filters={"remarks": ("like", f"%{tag}%"), "docstatus": 0}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True)

		prep = frappe.get_doc("Resto Prep", forecast.make_prep(frappe.as_json(filters)))
		made = {r.item_code: r.qty for r in prep.items}
		self.assertAlmostEqual(made[self.patty], 0.38)
		transfer = frappe.get_doc("Stock Entry", forecast.make_transfer(frappe.as_json(filters)))
		moved = {r.item_code: r.qty for r in transfer.items}
		self.assertAlmostEqual(moved[self.meat], 0.356)
		self.assertAlmostEqual(moved[self.bun], 2)
		self.assertEqual(transfer.docstatus, 0)
		# The same forecast again: the drafts already exist.
		with self.assertRaises(frappe.ValidationError):
			forecast.make_prep(frappe.as_json(filters))
		with self.assertRaises(frappe.ValidationError):
			forecast.make_transfer(frappe.as_json(filters))
		prep.delete()
		transfer.delete()
		self.assertEqual(getdate(TARGET).weekday(), 1)
