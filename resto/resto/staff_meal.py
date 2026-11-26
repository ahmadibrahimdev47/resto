"""Staff meals (D-231): what the staff eat, taken from the kitchen's stock at cost, with no sale.

A staff meal is a Resto Order marked as one, started by a cashier or a manager in their own open
shift. It is made like any order: menu dishes with their sides chosen as for a guest (a side taken
off is not deducted), and free ingredients, any stock item in its stock unit, for what is not on the
menu. A free ingredient in an unusual quantity (more than ten times what one dish of any active
recipe uses, or an item no recipe uses) needs a manager's approval, so grams typed as kilos never
leave the stock unnoticed.

Send and Post does both at once, so the kitchen gets the meal and the stock is taken in the same
step, and nobody has to remember to post it later: the dishes and the free ingredients go to the
kitchen on tickets marked Staff Meal (its number is the order's number of the day, which no other
order shares), and one Material Issue takes from the kitchen warehouse what the dishes are made of
(each dish as its bill would deduct it, sides as served) plus the free ingredients, into the Staff
Meals Account of Resto Settings at the stock's own valuation, on the company's cost center. The
order is then Posted, with the cost the stock ledger gave. No Sales Invoice is made, so a staff meal
is in no sale, no food cost percentage and no bill count; the shift shows it apart, as information,
not cash.

Before Send and Post the meal is changed or cancelled like any order with nothing sent. After it,
only a manager cancels it (Cancel Staff Meal on the order), which cancels its Stock Entry: the
stock and the account come back as they were. The Stock Entry cannot be cancelled on its own.
"""

import math
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from resto.resto import approval, kitchen, shift
from resto.resto.doctype.resto_order.resto_order import ACTIVE
from resto.resto.utils import CASHIER, get_settings, publish, require_role

POSTED = "Posted"
# A free ingredient above this many times what one dish of any active recipe uses is unusual.
USUAL_TIMES = 10
MAX_FREE_LINES = 30


def _qty_precision():
	return frappe.get_precision("Stock Entry Detail", "qty") or 3


def _whole(item_code):
	uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	return bool(uom and cint(frappe.get_cached_value("UOM", uom, "must_be_whole_number")))


def _own_shift():
	"""The session user's open shift: a staff meal is always made and posted in one."""
	name = shift.open_shift_of(frappe.session.user, get_settings().pos_profile)
	if not name:
		frappe.throw(_("Open your shift first: a staff meal is recorded in your shift"), title=_("No Open Shift"))
	return name


def account():
	"""The Staff Meals Account of Resto Settings, checked."""
	settings = get_settings()
	name = settings.get("staff_meal_account")
	if not name:
		frappe.throw(_("Set the Staff Meals Account in Resto Settings first"))
	company, is_group, root_type = frappe.db.get_value("Account", name, ["company", "is_group", "root_type"]) or (None, None, None)
	if company != settings.company or cint(is_group) or root_type != "Expense":
		frappe.throw(_("The Staff Meals Account {0} must be an expense account of company {1}, not a group").format(frappe.bold(name), settings.company))
	return name


def _staff_order(name, statuses=ACTIVE):
	doc = frappe.get_doc("Resto Order", name, for_update=True)
	if not cint(doc.staff_meal):
		frappe.throw(_("Order {0} is not a staff meal").format(doc.name))
	if statuses and doc.status not in statuses:
		frappe.throw(_("Order {0} is {1}").format(doc.name, _(doc.status, context="Resto")))
	return doc


def _save(doc):
	from resto.resto.floor import _save

	return _save(doc)


def _view(doc):
	from resto.resto.floor import _order_view

	return _order_view(doc)


# --------------------------------------------------------------------------- the floor API


@frappe.whitelist(methods=["POST"])
def create(staff_for=None):
	"""A new staff meal, opened like an order; staff_for: for whom (optional)."""
	require_role(CASHIER)
	_own_shift()
	account()
	doc = frappe.new_doc("Resto Order")
	doc.update({"order_type": "Takeaway", "staff_meal": 1, "staff_for": (staff_for or "").strip()[:140] or None})
	doc.flags.ignore_permissions = True
	doc.flags.resto_api = True
	doc.insert()
	return doc.name


def usual_qty(item_code):
	"""The most one dish (or one unit of a prepared ingredient) of any active recipe uses, or None."""
	value = frappe.db.sql(
		"""select max(ri.stock_qty / if(r.quantity > 0, r.quantity, 1))
		from `tabResto Recipe Item` ri join `tabResto Recipe` r on r.name = ri.parent
		where ri.parenttype = 'Resto Recipe' and ri.parentfield in ('items', 'sides') and r.is_active = 1
			and ri.item_code = %s""",
		item_code,
	)[0][0]
	return flt(value) if value and flt(value) > 0 else None


def _free_item(item_code):
	item = frappe.db.get_value(
		"Item", item_code, ["item_name", "stock_uom", "disabled", "is_stock_item", "has_variants", "has_batch_no", "has_serial_no"], as_dict=True
	)
	if not item:
		frappe.throw(_("Item {0} not found").format(item_code))
	name = frappe.bold(item.item_name or item_code)
	if item.disabled or item.has_variants:
		frappe.throw(_("{0} is disabled or is a template").format(name))
	if not item.is_stock_item:
		frappe.throw(_("{0} is not a stock item: a menu dish is added from the menu").format(name))
	if item.has_batch_no or item.has_serial_no:
		frappe.throw(_("{0} has batch or serial numbers, which a staff meal cannot choose").format(name))
	return item


@frappe.whitelist(methods=["POST"])
def set_item(order, item_code, qty, approver=None, pin=None):
	"""Set a free ingredient of a staff meal to qty (its stock unit); 0 takes it off."""
	require_role(CASHIER)
	_own_shift()
	doc = _staff_order(order)
	qty = flt(qty)
	if qty < 0:
		frappe.throw(_("The quantity cannot be negative"))
	row = next((d for d in doc.staff_items if d.item_code == item_code), None)
	if not qty:
		if row:
			doc.remove(row)
		return _view(_save(doc))
	item = _free_item(item_code)
	if _whole(item_code):
		if abs(qty - round(qty)) > 1e-9:
			frappe.throw(_("{0} is counted in whole units ({1})").format(frappe.bold(item.item_name), _(item.stock_uom)))
		qty = round(qty)
	else:
		qty = flt(qty, _qty_precision())
		if qty <= 0:
			frappe.throw(_("The quantity is too small"))
	if not row and len(doc.staff_items) >= MAX_FREE_LINES:
		frappe.throw(_("A staff meal has at most {0} free ingredients").format(MAX_FREE_LINES))
	usual = usual_qty(item_code)
	approved_by = None
	if usual is None or qty > usual * USUAL_TIMES + 1e-9:
		# Grams typed as kilos, or an item no recipe uses: a manager looks at it first.
		what = f"{doc.name}: {frappe.format(qty, 'Float')} {_(item.stock_uom)} {item.item_name}"
		details = (
			_("No active recipe uses {0}").format(item.item_name)
			if usual is None
			else _("One dish uses at most {0} {1}").format(frappe.format(usual, "Float"), _(item.stock_uom))
		)
		approved_by = approval.approve("staff_meal", what, 0, approver, pin, details)
	if row:
		row.qty = qty
		row.approved_by = approved_by
	else:
		doc.append("staff_items", {"item_code": item_code, "item_name": item.item_name, "qty": qty, "uom": item.stock_uom, "approved_by": approved_by})
	return _view(_save(doc))


def stock_needed(doc):
	"""{stock item: qty} a staff meal takes: each dish not void as its bill would deduct it (its
	recipe, the sides served, priced options that are stock), and the free ingredients."""
	from resto.resto.doctype.resto_waste.resto_waste import _components
	from resto.resto.floor import _waste_items

	need = defaultdict(float)
	for d in doc.items:
		if d.kitchen_status == "Void":
			continue
		items, _taken = _waste_items(doc, d, flt(d.qty))
		for code, qty in items.items():
			for c, q in _components(code, qty):
				need[c] += q
	for r in doc.staff_items:
		need[r.item_code] += flt(r.qty)
	precision = _qty_precision()
	out = {}
	for code, qty in need.items():
		# Half a whole unit taken is a unit gone from the shelf.
		qty = math.ceil(qty - 1e-6) if _whole(code) else flt(qty, precision)
		if qty > 0:
			out[code] = qty
	return out


def _stock_entry(doc, need, expense_account):
	settings = get_settings()
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Issue"
	se.purpose = "Material Issue"
	se.company = doc.company
	se.from_warehouse = settings.kitchen_warehouse
	who = f" ({doc.staff_for})" if doc.staff_for else ""
	se.remarks = _("Staff meal {0}{1}").format(doc.name, who)
	cost_center = frappe.get_cached_value("Company", doc.company, "cost_center")
	for code, qty in need.items():
		uom = frappe.get_cached_value("Item", code, "stock_uom")
		se.append(
			"items",
			{
				"item_code": code,
				"qty": qty,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"s_warehouse": settings.kitchen_warehouse,
				"expense_account": expense_account,
				"cost_center": cost_center,
			},
		)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se


def _posted_cost(stock_entry):
	value = frappe.db.sql(
		"""select sum(-stock_value_difference) from `tabStock Ledger Entry`
		where voucher_type = 'Stock Entry' and voucher_no = %s and is_cancelled = 0""",
		stock_entry,
	)[0][0]
	return flt(value, 2)


def _free_rows(doc):
	"""{station: [ticket rows]}: the free ingredients as text rows, each on its station's ticket."""
	out = {}
	for r in doc.staff_items:
		station = kitchen.get_station(r.item_code)
		if not station:
			frappe.throw(_("No kitchen station makes {0}. Set a default station first.").format(frappe.bold(r.item_name)))
		out.setdefault(station, []).append(
			{"item_code": r.item_code, "item_name": r.item_name, "qty": r.qty, "modifiers_text": _(r.uom), "notes": _("Free ingredient")}
		)
	return out


@frappe.whitelist(methods=["POST"])
def post(order):
	"""Send and Post: the meal to the kitchen and its stock out, in one step. A second press after a
	lost answer finds it posted and changes nothing."""
	require_role(CASHIER)
	doc = frappe.get_doc("Resto Order", order, for_update=True)
	if not cint(doc.staff_meal):
		frappe.throw(_("Order {0} is not a staff meal").format(doc.name))
	if doc.status == POSTED:
		return {"order": doc.name, "stock_entry": doc.staff_stock_entry, "cost": flt(doc.staff_cost), "tickets": []}
	if doc.status not in ACTIVE:
		frappe.throw(_("Order {0} is {1}").format(doc.name, _(doc.status, context="Resto")))
	shift_name = _own_shift()
	expense_account = account()
	need = stock_needed(doc)
	if not need:
		frappe.throw(_("Add what the staff eat first"))
	# The stock first: a shortage stops it before anything reaches the kitchen.
	se = _stock_entry(doc, need, expense_account)
	for d in doc.items:
		if d.kitchen_status == "Not Sent":
			d.hold = 0  # a staff meal is made at once, all of it
	doc.flags.resto_staff_rows = _free_rows(doc)
	tickets = kitchen.send_lines(doc)
	left = doc.flags.resto_staff_rows
	if left:
		# Stations that make only free ingredients of this meal.
		if not tickets:
			doc.send_rounds = cint(doc.get("send_rounds")) + 1
		now = now_datetime()
		for station in sorted(list(left)):
			tickets.append(kitchen._make_ticket(doc, station, "Order", [], now))
	doc.status = POSTED
	doc.closed_at = now_datetime()
	doc.resto_shift = shift_name
	doc.staff_stock_entry = se.name
	doc.staff_cost = _posted_cost(se.name)
	_save(doc)
	doc.add_comment("Info", _("Staff meal posted: {0}, cost {1}").format(se.name, frappe.format(doc.staff_cost, "Currency")))
	publish("shift")
	return {"order": doc.name, "stock_entry": se.name, "cost": flt(doc.staff_cost), "tickets": [t.name for t in tickets]}


@frappe.whitelist(methods=["POST"])
def cancel(order, reason):
	"""A manager cancels a posted staff meal: its Stock Entry is cancelled, so the stock and the
	account come back; the kitchen's open tickets of it are cancelled."""
	require_role()  # a manager
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason"))
	doc = _staff_order(order, statuses=(POSTED,))
	if doc.staff_stock_entry and frappe.db.get_value("Stock Entry", doc.staff_stock_entry, "docstatus") == 1:
		se = frappe.get_doc("Stock Entry", doc.staff_stock_entry)
		se.flags.ignore_permissions = True
		se.flags.resto_staff_meal = True
		se.cancel()
	doc.status = "Cancelled"
	doc.cancel_reason = reason
	doc.flags.resto_allow_closed = True
	_save(doc)
	doc.add_comment("Info", _("Staff meal cancelled: {0}").format(reason))
	publish("shift")
	return doc.name


def check_cancel(doc, method=None):
	"""Stock Entry before cancel: a posted staff meal's entry is cancelled from the meal itself."""
	if doc.flags.get("resto_staff_meal"):
		return
	order = frappe.db.get_value("Resto Order", {"staff_stock_entry": doc.name, "status": POSTED}, "name")
	if order:
		frappe.throw(
			_("This Stock Entry is staff meal {0}: a manager cancels it from the order (Cancel Staff Meal), so the order and the shift follow").format(order)
		)


# --------------------------------------------------------------------------- the shift


def of_shift(shift_name):
	"""The shift's posted staff meals: [{name, order_no, staff_for, staff_cost, closed_at}]."""
	return frappe.get_all(
		"Resto Order",
		filters={"resto_shift": shift_name, "staff_meal": 1, "status": POSTED},
		fields=["name", "order_no", "staff_for", "staff_cost", "closed_at"],
		order_by="closed_at asc",
	)
