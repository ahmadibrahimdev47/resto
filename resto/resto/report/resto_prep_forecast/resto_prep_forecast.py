"""Prep forecast: how many of each dish to expect on a day, and what the kitchen needs for it.

Expected dishes: the same weekday over the last N weeks, before today (open days only: a day with
no sale at all counts as closed and is skipped), as a plain average, a weighted average (recent
weeks count more) or an average without the highest and lowest day, plus a safety margin. A new
dish is averaged only over the days since it was first sold; disabled and non-sales items are left
out.

Needed ingredients: the expected dishes expanded through their recipes (sides included; a dish
used inside another dish brings its main ingredients). Prepared ingredients are compared with the
kitchen stock; what is missing is to be prepared, and its own recipe adds the raw materials it
needs. Raw materials are compared with the kitchen and the main store: to transfer, or to buy.

Stock is the stock now. For today, only what is still expected today counts (the kitchen stock
already lost what was sold). For tomorrow, what is still expected today is set aside first. For a
later day the stock may change before then; the message says so.

Quantities to make, transfer or buy are rounded up: to a whole number for a unit that must be
whole, else to 3 decimals.
"""

import math
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from resto.resto.demand import WEEKDAYS, business_today, first_sale_dates, open_days, sales_by_day
from resto.resto.doctype.resto_recipe.resto_recipe import MENU, PREPARED, get_active_recipe, get_flat_components
from resto.resto.utils import get_settings

METHODS = ("Average", "Weighted Average", "Average without Highest and Lowest")
EPS = 1e-9
TRIM_MIN_DAYS = 4


def execute(filters=None):
	plan = get_plan(filters)
	if not plan.used:
		return [], [], plan.message
	if plan.filters.view == "Ingredients":
		columns, data = ingredient_table(plan)
	else:
		columns, data = dish_table(plan)
	return columns, data, plan.message, None, summary(plan)


def prepare(filters):
	f = frappe._dict(filters or {})
	settings = get_settings()
	f.company = f.company or settings.company
	f.today = business_today()
	f.target_date = getdate(f.target_date or add_days(f.today, 1))
	f.weeks = min(max(cint(f.weeks) or 6, 1), 26)
	f.method = f.method if f.method in METHODS else "Average"
	# The page drops a 0 filter, so an absent margin is 0 (the page's default is 10).
	f.safety_margin = max(flt(f.safety_margin), 0)
	f.view = "Ingredients" if f.view == "Ingredients" else "Dishes"
	f.kitchen = company_warehouse(settings.kitchen_warehouse, f.company)
	f.main = company_warehouse(settings.main_warehouse, f.company)
	return f


def company_warehouse(warehouse, company):
	if warehouse and frappe.get_cached_value("Warehouse", warehouse, "company") == company:
		return warehouse
	return None


# --------------------------------------------------------------------------- dishes


def forecast(f, day, item_group=None):
	"""(days looked at, used days, dishes) for one day: dishes carry by_date, base and expected."""
	# The last N same weekdays before today (a day weeks ahead still gets N weeks of history).
	first = max(1, math.ceil(((day - f.today).days + 1) / 7))
	dates = [add_days(day, -7 * k) for k in range(first, first + f.weeks)]
	opened = set(open_days(f.company, dates[-1], dates[0]))
	used = [d for d in dates if d in opened]  # most recent first
	if not used:
		return dates, [], []

	history = defaultdict(lambda: defaultdict(float))
	names = {}
	for r in sales_by_day(f.company, used[-1], used[0], item_group=item_group, dates=used):
		history[r.item][getdate(r.posting_date)] += flt(r.qty)
		names[r.item] = r.item_name

	items = list(history)
	info = {
		r.name: r
		for r in frappe.get_all("Item", filters={"name": ["in", items or [""]]}, fields=["name", "disabled", "is_sales_item"])
	}
	first = first_sale_dates(f.company, items)

	dishes = []
	for item, by_date in history.items():
		i = info.get(item)
		if not i or i.disabled or not i.is_sales_item:
			continue
		# A new dish: only the days since it was first sold.
		since = first.get(item)
		item_used = [d for d in used if not since or d >= since]
		series = [by_date.get(d, 0.0) for d in item_used]
		base = estimate(series, f.method)
		expected = base * (1 + f.safety_margin / 100)
		if expected <= EPS:
			continue
		dishes.append(frappe._dict(item=item, item_name=names.get(item), by_date=dict(by_date), days=len(item_used),
			new=len(item_used) < len(used), base=base, expected=expected))
	dishes.sort(key=lambda d: -d.expected)
	return dates, used, dishes


def estimate(series, method):
	"""series: quantity per used day, most recent first."""
	if not series:
		return 0.0
	if method == "Weighted Average":
		weights = list(range(len(series), 0, -1))
		return sum(q * w for q, w in zip(series, weights)) / sum(weights)
	if method == "Average without Highest and Lowest" and len(series) >= TRIM_MIN_DAYS:
		series = sorted(series)[1:-1]
	return sum(series) / len(series)


def sold_today(f):
	return {r.item: flt(r.qty) for r in sales_by_day(f.company, f.today, f.today)}


def get_plan(filters):
	f = prepare(filters)
	plan = frappe._dict(filters=f, used=[], dishes=[], prepared=[], raw=[], no_recipe=[], reserved={}, notes=[])
	weekday = _(WEEKDAYS[f.target_date.weekday()])
	dates, plan.used, plan.dishes = forecast(f, f.target_date, f.item_group)
	if not plan.used:
		plan.message = _("No sales on the last {0} weeks' {1}s before {2}. There is no history to forecast from yet.").format(
			f.weeks, weekday, frappe.format(f.target_date, "Date")
		)
		return plan

	plan.is_today = f.target_date == f.today
	for d in plan.dishes:
		d.need = d.expected
	if plan.is_today:
		# The kitchen stock already lost what was sold today: plan only what is still expected.
		sold = sold_today(f)
		for d in plan.dishes:
			d.sold = sold.get(d.item, 0.0)
			d.need = max(d.expected - d.sold, 0)
		plan.notes.append(_("For today: only what is still expected after today's sales so far."))
	elif f.target_date == add_days(f.today, 1) and f.kitchen:
		# The rest of today's sales will use kitchen stock before tomorrow.
		_dates, _used, today_dishes = forecast(f, f.today)
		sold = sold_today(f)
		rest = [frappe._dict(item=d.item, need=max(d.expected - sold.get(d.item, 0), 0)) for d in today_dishes]
		plan.reserved, _none = components(rest)
		if plan.reserved:
			plan.notes.append(_("Kitchen stock still needed by the rest of today's expected sales is set aside first."))
	elif f.target_date > f.today:
		plan.notes.append(_("Stock is as of now and may change before {0}.").format(frappe.format(f.target_date, "Date")))
	else:
		plan.notes.append(_("A past day: stock is as of now, not as of that day."))

	explode(plan)

	plan.message = _("Forecast for {0} ({1}) from {2} of the last {3} {1}s: {4}.").format(
		frappe.format(f.target_date, "Date"), weekday, len(plan.used), len(dates),
		", ".join(frappe.format(d, "Date") for d in plan.used),
	)
	if f.method == METHODS[2] and len(plan.used) < TRIM_MIN_DAYS:
		plan.notes.append(_("Fewer than {0} days: the plain average is used.").format(TRIM_MIN_DAYS))
	new = [d.item_name or d.item for d in plan.dishes if d.new]
	if new:
		plan.notes.append(_("New dishes, averaged since they were first sold: {0}.").format(", ".join(new)))
	if plan.no_recipe:
		names = {d.item: d.item_name or d.item for d in plan.dishes}
		plan.notes.append(_("Dishes without a recipe are not in the ingredients: {0}.").format(
			", ".join(frappe.bold(names.get(i, i)) for i in plan.no_recipe)))
	if not f.kitchen or not f.main:
		plan.notes.append(_("Set the Main Store and the Kitchen Warehouse of this company in Restaurant Settings to compare with stock."))
	plan.message = " ".join([plan.message, *plan.notes])
	return plan


# --------------------------------------------------------------------------- ingredients


def whole(uom):
	return bool(uom and frappe.get_cached_value("UOM", uom, "must_be_whole_number"))


def round_up(qty, uom):
	"""Up to a whole number for a whole unit, else up to 3 decimals; never below 0."""
	if qty <= EPS:
		return 0.0
	if whole(uom):
		return float(math.ceil(qty - 1e-6))
	return math.ceil(qty * 1000 - 1e-6) / 1000


def components(dishes):
	"""First-level stock needs of dishes (each with item and need): {item_code: qty}, no-recipe items."""
	need, no_recipe = defaultdict(float), []
	for d in dishes:
		recipe = get_active_recipe(d.item, MENU)
		d.source = "Recipe" if recipe else "Stock" if frappe.get_cached_value("Item", d.item, "is_stock_item") else ""
		if not d.source:
			no_recipe.append(d.item)
		if d.need <= EPS:
			continue
		if recipe:
			doc = frappe.get_cached_doc("Resto Recipe", recipe)
			per_portion = 1 / (flt(doc.quantity) or 1)
			for item_code, qty in get_flat_components(doc).items():
				need[item_code] += d.need * flt(qty) * per_portion
		elif d.source == "Stock":
			need[d.item] += d.need  # sold from stock as it is (a bottled drink)
	return dict(need), no_recipe


def explode(plan):
	f = plan.filters
	first_level, plan.no_recipe = components(plan.dishes)
	need = defaultdict(float, first_level)
	kitchen = stock(f.kitchen)
	main = stock(f.main)
	recipes, uoms = {}, {}

	def uom(item_code):
		if item_code not in uoms:
			uoms[item_code] = frappe.get_cached_value("Item", item_code, "stock_uom")
		return uoms[item_code]

	def available(item_code):
		return max(kitchen.get(item_code, 0) - plan.reserved.get(item_code, 0), 0)

	def prepared_recipe(item_code):
		if item_code not in recipes:
			name = get_active_recipe(item_code, PREPARED)
			recipes[item_code] = frappe.get_cached_doc("Resto Recipe", name) if name else None
		return recipes[item_code]

	# What is missing of a prepared ingredient is made from its own recipe, which may use other
	# prepared ingredients: repeat until nothing more is added (recipes cannot loop).
	made = defaultdict(float)
	for _round in range(50):
		changed = False
		for item_code in list(need):
			recipe = prepared_recipe(item_code)
			if not recipe:
				continue
			to_make = round_up(need[item_code] - available(item_code), uom(item_code))
			delta = to_make - made[item_code]
			if delta <= EPS:
				continue
			yield_qty = flt(recipe.quantity) or 1
			for row in recipe.items:
				need[row.item_code] += delta * flt(row.stock_qty) / yield_qty
			made[item_code] = to_make
			changed = True
		if not changed:
			break

	for item_code, required in need.items():
		if required <= EPS:
			continue
		info = frappe.get_cached_value("Item", item_code, ["item_name", "is_stock_item"], as_dict=True) or {}
		unit = uom(item_code)
		row = frappe._dict(item=item_code, item_name=info.get("item_name"), uom=unit, required=required,
			in_kitchen=kitchen.get(item_code, 0), reserved=plan.reserved.get(item_code, 0))
		recipe = prepared_recipe(item_code)
		if recipe:
			row.recipe = recipe.name
			row.to_prepare = made.get(item_code, 0)
			plan.prepared.append(row)
		elif info.get("is_stock_item"):
			row.in_main = main.get(item_code, 0)
			short = round_up(required - available(item_code), unit)
			# Never more than the main store has, at the precision a Stock Entry keeps.
			movable = max(row.in_main, 0)
			movable = float(math.floor(movable + 1e-6)) if whole(unit) else math.floor(movable * 1000 + 1e-6) / 1000
			row.to_transfer = min(short, movable)
			row.to_buy = round_up(short - row.to_transfer, unit)
			plan.raw.append(row)
	plan.prepared.sort(key=lambda r: (r.item_name or r.item))
	plan.raw.sort(key=lambda r: (r.item_name or r.item))


def stock(warehouse):
	if not warehouse:
		return {}
	return {
		r.item_code: flt(r.actual_qty)
		for r in frappe.get_all("Bin", filters={"warehouse": warehouse}, fields=["item_code", "actual_qty"])
	}


# --------------------------------------------------------------------------- tables


def qty_col(fieldname, label, width=110):
	return {"fieldname": fieldname, "label": label, "fieldtype": "Float", "precision": 3, "width": width}


def dish_table(plan):
	columns = [
		{"fieldname": "item", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 180},
	]
	for i, d in enumerate(plan.used):
		columns.append(qty_col(f"d{i}", frappe.format(d, "Date"), 95))
	columns += [
		qty_col("base", _(plan.filters.method, context="Resto"), 120),
		qty_col("expected", _("Expected Qty"), 120),
	]
	if plan.is_today:
		columns += [qty_col("sold", _("Sold So Far")), qty_col("need", _("Still Expected"))]
	columns += [
		{"fieldname": "source", "label": _("Ingredients From"), "fieldtype": "Data", "width": 120},
		{"fieldname": "note", "label": _("Note"), "fieldtype": "Data", "width": 150},
	]
	data = []
	for d in plan.dishes:
		row = {"item": d.item, "item_name": d.item_name, "base": d.base, "expected": d.expected,
			"sold": d.get("sold"), "need": d.need,
			"source": {"Recipe": _("Recipe"), "Stock": _("From Stock")}.get(d.get("source"), _("No Recipe")),
			"note": _("New: {0} days").format(d.days) if d.new else ""}
		for i, day in enumerate(plan.used):
			# Before a new dish was first sold: blank, not 0.
			row[f"d{i}"] = d.by_date.get(day, 0.0) if i < d.days else None
		data.append(row)
	return columns, data


def ingredient_table(plan):
	columns = [
		{"fieldname": "kind", "label": _("Type"), "fieldtype": "Data", "width": 130},
		{"fieldname": "item", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "uom", "label": _("UOM"), "fieldtype": "Link", "options": "UOM", "width": 70},
		qty_col("required", _("Required Qty")),
		qty_col("in_kitchen", _("In Kitchen")),
	]
	if plan.reserved:
		columns.append(qty_col("reserved", _("Still Needed Today")))
	columns += [
		qty_col("to_prepare", _("To Prepare")),
		qty_col("in_main", _("In Main Store")),
		qty_col("to_transfer", _("To Transfer")),
		qty_col("to_buy", _("To Buy")),
	]
	data = []
	for r in plan.prepared:
		data.append({**r, "kind": _("Prepared Ingredient"), "in_main": None, "to_transfer": None, "to_buy": None})
	for r in plan.raw:
		data.append({**r, "kind": _("Raw Material"), "to_prepare": None})
	return columns, data


def summary(plan):
	f = plan.filters
	return [
		{"label": _("Days Used"), "value": len(plan.used), "datatype": "Int"},
		{"label": _("Expected Dishes"), "value": sum(d.need for d in plan.dishes), "datatype": "Float", "indicator": "Blue"},
		{"label": _("Safety Margin %"), "value": f.safety_margin, "datatype": "Percent"},
		{"label": _("Prepared Ingredients to Prepare"), "value": sum(1 for r in plan.prepared if r.to_prepare > EPS), "datatype": "Int", "indicator": "Orange"},
		{"label": _("Raw Materials to Transfer"), "value": sum(1 for r in plan.raw if r.to_transfer > EPS), "datatype": "Int"},
		{"label": _("Raw Materials to Buy"), "value": sum(1 for r in plan.raw if r.to_buy > EPS), "datatype": "Int", "indicator": "Red"},
		{"label": _("Dishes without a Recipe"), "value": len(plan.no_recipe), "datatype": "Int"},
	]


# --------------------------------------------------------------------------- actions


def plan_for_draft(filters):
	f = frappe._dict(frappe.parse_json(filters) or {})
	if f.get("item_group"):
		frappe.throw(_("Clear the Item Group filter first: a draft must cover every dish."))
	plan = get_plan(f)
	if not plan.used:
		frappe.throw(plan.message)
	return plan


def draft_tag(plan):
	"""Marks a draft with its forecast day, the same in every language and date format."""
	return f"[RPF {plan.filters.target_date.isoformat()}]"


def draft_remark(plan):
	return _("Prep forecast for {0}").format(frappe.format(plan.filters.target_date, "Date")) + " " + draft_tag(plan)


def refuse_duplicate(doctype, plan, **filters):
	filters.update(company=plan.filters.company, remarks=("like", f"%{draft_tag(plan)}%"), docstatus=0)
	name = frappe.db.get_value(doctype, filters, "name")
	if name:
		frappe.throw(_("A draft for this forecast already exists: {0}. Open it, or delete it to make a new one.").format(
			frappe.utils.get_link_to_form(doctype, name)))


@frappe.whitelist(methods=["POST"])
def make_prep(filters):
	"""Draft Daily Prep with what is missing of each prepared ingredient."""
	frappe.has_permission("Resto Prep", "create", throw=True)
	plan = plan_for_draft(filters)
	f = plan.filters
	if not f.kitchen:
		frappe.throw(_("Set the Kitchen Warehouse of this company in Restaurant Settings first"))
	rows = [r for r in plan.prepared if r.to_prepare > EPS]
	if not rows:
		frappe.msgprint(_("The kitchen has enough of every prepared ingredient for this forecast."))
		return None
	remark = draft_remark(plan)
	refuse_duplicate("Resto Prep", plan)
	prep = frappe.new_doc("Resto Prep")
	prep.company = f.company
	prep.warehouse = f.kitchen
	prep.source_warehouse = f.main
	prep.remarks = remark
	for r in rows:
		prep.append("items", {"recipe": r.recipe, "item_code": r.item, "current_qty": r.in_kitchen,
			"suggested_qty": r.to_prepare, "qty": r.to_prepare})
	prep.insert()
	return prep.name


@frappe.whitelist(methods=["POST"])
def make_transfer(filters):
	"""Draft Material Transfer from the main store to the kitchen for the raw materials to transfer."""
	frappe.has_permission("Stock Entry", "create", throw=True)
	plan = plan_for_draft(filters)
	f = plan.filters
	if not f.kitchen or not f.main:
		frappe.throw(_("Set the Main Store and the Kitchen Warehouse of this company in Restaurant Settings first"))
	rows = [r for r in plan.raw if r.to_transfer > EPS]
	if not rows:
		frappe.msgprint(_("Nothing to transfer: the kitchen has the raw materials, or the main store has none of the missing ones."))
		return None
	remark = draft_remark(plan)
	refuse_duplicate("Stock Entry", plan, purpose="Material Transfer")
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer"
	se.purpose = "Material Transfer"
	se.company = f.company
	se.from_warehouse = f.main
	se.to_warehouse = f.kitchen
	se.remarks = remark
	for r in rows:
		se.append("items", {"item_code": r.item, "qty": r.to_transfer, "uom": r.uom, "stock_uom": r.uom,
			"conversion_factor": 1, "s_warehouse": f.main, "t_warehouse": f.kitchen})
	se.insert()
	return se.name
