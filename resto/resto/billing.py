"""Turning a Resto Order into a standard POS Sales Invoice.

The invoice is built in memory with ERPNext's own code (POS Profile fields, payments, taxes,
discount, totals), so the bill preview and the final invoice always agree. No ERPNext or POSNext
class is overridden.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from resto.resto import sides
from resto.resto.utils import CASHIER, get_settings, has_role, posnext_installed, publish, require_role

EPS = 0.0000001


# --------------------------------------------------------------------------- building


def lines_to_bill(order, lines=None):
	"""Return [(row, qty)] to bill. `lines` maps order line name -> qty; None bills everything left."""
	unbilled = {d.name: d for d in order.unbilled_lines()}
	if lines is None:
		return [(d, flt(d.qty) - flt(d.billed_qty)) for d in unbilled.values()]

	if isinstance(lines, str):
		lines = json.loads(lines)
	result = []
	for name, qty in (lines or {}).items():
		qty = flt(qty)
		if qty <= 0:
			continue
		row = unbilled.get(name)
		if not row:
			frappe.throw(_("A selected line is already billed or void"))
		left = flt(row.qty) - flt(row.billed_qty)
		if qty > left + EPS:
			frappe.throw(
				_("Only {0} of {1} is left to bill").format(flt(left), frappe.bold(row.item_name))
			)
		result.append((row, qty))
	if not result:
		frappe.throw(_("Choose at least one line to bill"))
	return result


def is_last_part(order, to_bill):
	"""True when this invoice bills everything that is still unbilled."""
	billing = {row.name: qty for row, qty in to_bill}
	for row in order.unbilled_lines():
		if flt(row.qty) - flt(row.billed_qty) - billing.get(row.name, 0) > EPS:
			return False
	return True


def build_invoice(order, lines=None):
	"""An unsaved, fully calculated POS Sales Invoice for the order (or part of it)."""
	settings = get_settings()
	if not order.pos_profile:
		frappe.throw(_("Set the POS Profile in Resto Settings first"))
	profile = frappe.get_cached_doc("POS Profile", order.pos_profile)

	to_bill = lines_to_bill(order, lines)
	if not to_bill:
		frappe.throw(_("Nothing left to bill on this order"))
	active_invoices = [d.sales_invoice for d in order.invoices if not d.cancelled]
	last_part = is_last_part(order, to_bill)

	si = frappe.new_doc("Sales Invoice")
	# Waiters and cashiers bill through Resto; they need no rights on customers or accounts.
	si.flags.ignore_permissions = True
	si.company = order.company
	si.is_pos = 1
	si.pos_profile = order.pos_profile
	# Resto prices are final: no pricing rules on top of them (also no free items added below).
	si.ignore_pricing_rule = 1
	# Same rounding as POSNext uses for this POS Profile.
	si.disable_rounded_total = cint(profile.get("disable_rounded_total"))
	si.customer = order.customer or profile.customer
	if not si.customer:
		frappe.throw(_("Set a default customer in POS Profile {0}").format(frappe.bold(order.pos_profile)))
	si.posting_date = frappe.utils.nowdate()
	si.posting_time = frappe.utils.nowtime()

	prices = []  # (rate) per appended invoice row, to re-apply after ERPNext fills defaults
	food_total = 0
	extras = {}  # (item, price, set or line) -> extra portions billed apart (sides.py)
	for row, qty in to_bill:
		description = row.item_name
		if row.modifiers_text:
			description += f" ({row.modifiers_text})"
		if row.notes:
			description += f" - {row.notes}"
		if cint(order.get("print_sides")) and row.sides_text:
			description += f" - {row.sides_text}"
		_append_item(si, row.item_code, qty, flt(row.rate), description)
		if row.get("sides"):
			# Changed sides, as frozen when the line went to the kitchen: the invoice's validate
			# hook packs them instead of the recipe's (sides.py).
			served = row.get("sides_qty") or json.dumps(sides.qty_per_dish(row.item_code, json.loads(row.sides)), sort_keys=True)
			si.items[-1].resto_sides = served
		prices.append(flt(row.rate))
		food_total += qty * flt(row.rate)
		for mod in json.loads(row.modifiers or "[]"):
			if flt(mod.get("price")) and mod.get("item"):
				_append_item(si, mod["item"], qty, flt(mod["price"]), f"{mod['option']} - {row.item_name}")
				prices.append(flt(mod["price"]))
				food_total += qty * flt(mod["price"])
		# Extra portions above the recipe's sides, as frozen when the line went to the kitchen: their
		# own line at the option's extra price, deducting their own item (sides.py, D-191). Extras
		# of a choice shared by several dishes are one line.
		for e in json.loads(row.get("sides_extras") or "{}").values():
			extra_qty = flt(e.get("portions")) * flt(qty)
			if extra_qty <= EPS or not flt(e.get("price")):
				continue
			x = extras.setdefault((e["item"], flt(e["price"]), e.get("set") or row.name), {"qty": 0, "name": e.get("name") or e["item"], "dishes": []})
			x["qty"] += extra_qty
			if row.item_name not in x["dishes"]:
				x["dishes"].append(row.item_name)
	for (item_code, price, _key), x in extras.items():
		qty = flt(x["qty"], 6)
		sides.check_extra_item(item_code)
		if sides._whole(item_code) and abs(qty - round(qty)) > 1e-6:
			frappe.throw(
				_("{0}: the extra {1} is counted in whole units, so bill all of these dishes together").format(
					frappe.bold(sides._sep().join(x["dishes"])), frappe.bold(x["name"])
				)
			)
		_append_item(si, item_code, qty, price, sides._t("{0} extra - {1}", x["name"], sides._sep().join(x["dishes"])))
		prices.append(price)
		food_total += qty * price

	# The fee goes on one invoice: the first one that is not cancelled.
	fee_billed = bool(
		active_invoices
		and settings.delivery_fee_item
		and frappe.db.exists("Sales Invoice Item", {"parent": ("in", active_invoices), "item_code": settings.delivery_fee_item})
	)
	delivery_fee = flt(order.delivery_fee) if order.order_type == "Delivery" and not fee_billed else 0
	if delivery_fee:
		if not settings.delivery_fee_item:
			frappe.throw(_("Set the Delivery Fee Item in Resto Settings first"))
		_append_item(si, settings.delivery_fee_item, 1, delivery_fee, _("Delivery Fee"))
		prices.append(delivery_fee)

	# ERPNext fills accounts, warehouse, cost center, payments and the tax template from the
	# POS Profile.
	si.set_missing_values()
	# The POS Profile may switch pricing rules back on, and a "free item" rule adds rows.
	si.ignore_pricing_rule = 1
	free = [d for d in si.items if d.get("is_free_item")]
	if free:
		si.set("items", [d for d in si.items if not d.get("is_free_item")])
		for i, d in enumerate(si.items, 1):
			d.idx = i
	for item, rate in zip(si.items, prices, strict=True):
		item.price_list_rate = rate
		item.rate = rate
		item.discount_percentage = 0
		item.discount_amount = 0
		item.margin_type = ""
		item.margin_rate_or_amount = 0
		item.pricing_rules = ""

	# Discount: the order discount is worked out on food (not the delivery fee). A part bill gets
	# its share; the last part gets whatever is left so the parts add up exactly.
	# ERPNext spreads the invoice discount over every row, the fee row included; the totals and
	# the GL stay exact, only the split between food and fee income moves.
	discount = _discount_for_part(order, food_total, last_part, si)
	si.additional_discount_percentage = 0
	si.discount_amount = discount

	_set_service_charge(si, order, settings, food_total - discount if delivery_fee else None)
	inclusive = _set_tax_inclusive(si, profile)
	if inclusive and flt(order.service_charge_percent):
		frappe.throw(
			_("Prices include tax for POS Profile {0}, so a service charge cannot be added on top. Turn off tax inclusive prices or the service charge.").format(
				frappe.bold(order.pos_profile)
			)
		)
	# With prices that include tax, the discount comes off the price the guest sees.
	si.apply_discount_on = "Grand Total" if inclusive else "Net Total"

	guests_billed = active_invoices and frappe.db.sql(
		"select sum(resto_guests) from `tabSales Invoice` where name in %s", (active_invoices,)
	)[0][0]
	si.update(
		{
			"resto_order": order.name,
			"resto_order_type": order.order_type,
			"resto_table": order.resto_table,
			"resto_waiter": order.waiter,
			"resto_guests": 0 if cint(guests_billed) else cint(order.guests),
			"remarks": _("Restaurant order {0}").format(order.name),
		}
	)
	si.calculate_taxes_and_totals()
	si.flags.resto_lines = {row.name: qty for row, qty in to_bill}
	si.flags.resto_billing = True  # may carry Sides Served (sides.py)
	return si


def _append_item(si, item_code, qty, rate, description):
	item = frappe.get_cached_value("Item", item_code, ["item_name", "stock_uom"], as_dict=True)
	si.append(
		"items",
		{
			"item_code": item_code,
			"item_name": item.item_name,
			"description": description,
			"qty": qty,
			"uom": item.stock_uom,
			"stock_uom": item.stock_uom,
			"conversion_factor": 1,
			"rate": rate,
			"price_list_rate": rate,
		},
	)


def _discount_for_part(order, food_total, last_part, si):
	total_discount = flt(order.discount_amount)
	if not total_discount:
		return 0
	precision = si.precision("discount_amount")
	if last_part:
		given = 0
		names = [d.sales_invoice for d in order.invoices if not d.cancelled]
		if names:
			given = flt(
				frappe.db.sql(
					"select sum(discount_amount) from `tabSales Invoice` where name in %s and docstatus = 1",
					(names,),
				)[0][0]
			)
		discount = max(total_discount - given, 0)
	else:
		discount = food_total * total_discount / flt(order.net_total) if flt(order.net_total) else 0
	# Never more than the food on this part (items voided after an earlier part was paid).
	return flt(min(discount, food_total), precision)


def _set_service_charge(si, order, settings, actual_base=None):
	"""Service charge as a tax row on the service account.

	If the POS Profile's tax template already has a row on the service account, Resto only sets
	its rate (so the template decides the order of rows, e.g. VAT on top of service). Otherwise
	Resto adds the row: first, with the template's taxes charged on items plus service when
	"Charge VAT on Service" is on, or last when it is off.

	actual_base: when the bill has a delivery fee, service is charged on the food only, as a
	fixed amount (an "On Net Total" row would also charge service on the fee).
	"""
	percent = flt(order.service_charge_percent)
	account = settings.service_charge_account
	cost_center = si.cost_center or frappe.get_cached_value("Company", si.company, "cost_center")

	def fill(t):
		t.included_in_print_rate = 0
		if actual_base is not None:
			t.charge_type = "Actual"
			t.rate = 0
			t.tax_amount = flt(flt(actual_base) * percent / 100, t.precision("tax_amount"))
		else:
			t.charge_type = "On Net Total"
			t.rate = percent

	rows = [t for t in si.taxes if account and t.account_head == account]
	if rows:
		for t in rows:
			fill(t)
		return
	if not percent:
		return
	if not account:
		frappe.throw(_("Set the Service Charge Account in Resto Settings first"))
	row = si.append(
		"taxes",
		{"account_head": account, "description": _("Service Charge"), "cost_center": cost_center},
	)
	fill(row)
	if not cint(settings.get("vat_on_service")):
		return
	others = [t for t in si.taxes if t is not row]
	for t in others:
		if t.charge_type == "On Net Total":
			t.charge_type = "On Previous Row Total"
			t.row_id = "1"
		elif t.charge_type in ("On Previous Row Amount", "On Previous Row Total") and t.row_id:
			t.row_id = str(cint(t.row_id) + 1)
	si.set("taxes", [row, *others])
	for i, t in enumerate(si.taxes, 1):
		t.idx = i


def taxes_inclusive_setting(pos_profile):
	"""POSNext's tax inclusive switch for the profile, or None when POSNext does not decide it."""
	if not posnext_installed() or not frappe.db.has_column("POS Settings", "tax_inclusive"):
		return None
	return bool(cint(frappe.db.get_value("POS Settings", {"pos_profile": pos_profile}, "tax_inclusive")))


def _set_tax_inclusive(si, profile):
	"""Apply the same tax inclusive rule that POSNext's Sales Invoice hook applies on save, so the
	bill shown and the invoice saved are the same. Returns True when prices include tax."""
	setting = taxes_inclusive_setting(profile.name)
	if setting is not None:
		for t in si.taxes:
			t.included_in_print_rate = 0 if t.charge_type == "Actual" else int(setting)
	return any(cint(t.included_in_print_rate) for t in si.taxes)


def payable_amount(si):
	return flt(si.grand_total) if cint(si.disable_rounded_total) else flt(si.rounded_total or si.grand_total)


# --------------------------------------------------------------------------- preview


def bill_preview(order, lines=None):
	si = build_invoice(order, lines)
	service_account = get_settings().service_charge_account
	service = sum(flt(t.tax_amount_after_discount_amount) for t in si.taxes if t.account_head == service_account)
	taxes = [
		{"description": t.description, "rate": flt(t.rate), "amount": flt(t.tax_amount_after_discount_amount)}
		for t in si.taxes
		if flt(t.tax_amount_after_discount_amount)
	]
	payable = payable_amount(si)
	guests = max(cint(order.guests), 1)
	return {
		"items": [
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"description": d.description,
				"qty": flt(d.qty),
				"rate": flt(d.rate),
				"amount": flt(d.qty) * flt(d.rate),
			}
			for d in si.items
		],
		"total": flt(si.total),
		"discount": flt(si.discount_amount),
		"net_total": flt(si.net_total),
		"service": service,
		"taxes": taxes,
		"tax_total": flt(si.total_taxes_and_charges) - service,
		"grand_total": flt(si.grand_total),
		"rounding_adjustment": flt(si.rounding_adjustment),
		"payable": payable,
		"per_guest": flt(payable / guests, 2),
		"guests": guests,
		"currency": si.currency,
		"payment_modes": [
			{"mode_of_payment": p.mode_of_payment, "type": p.type, "default": cint(p.default)}
			for p in si.payments
		],
	}


# --------------------------------------------------------------------------- settle


def get_open_shift(user, pos_profile):
	"""The user's open POSNext shift for this POS Profile, if POSNext is installed."""
	if not posnext_installed() or not frappe.db.exists("DocType", "POS Opening Shift"):
		return None
	filters = {"user": user, "pos_profile": pos_profile, "docstatus": 1, "status": "Open"}
	if frappe.get_meta("POS Opening Shift").has_field("pos_closing_shift"):
		filters["pos_closing_shift"] = ("is", "not set")
	return frappe.db.get_value("POS Opening Shift", filters, "name", order_by="period_start_date desc")


def settle(order, payments, lines=None, expected=None, request_id=None):
	"""Create and submit the POS Sales Invoice, then mark the billed lines.

	expected: the amount the cashier saw; if the bill changed meanwhile (another device voided an
	item or gave a discount) nothing is billed. request_id: a retry of the same click returns the
	invoice already made instead of billing twice.
	"""
	require_role(CASHIER)
	settings = get_settings()
	if request_id:
		done = next((d for d in order.invoices if d.request_id == request_id and not d.cancelled), None)
		if done:
			return frappe.get_doc("Sales Invoice", done.sales_invoice)
	if order.status not in ("Open", "Bill Requested"):
		frappe.throw(_("Order {0} is {1}").format(order.name, _(order.status, context="Resto")))
	shift = get_open_shift(frappe.session.user, order.pos_profile)
	if cint(settings.require_pos_shift) and posnext_installed() and not shift:
		frappe.throw(
			_("Open a shift in POSNext for POS Profile {0} before settling bills").format(
				frappe.bold(order.pos_profile)
			)
		)

	si = build_invoice(order, lines)
	unsent = [d.item_name for d in order.items if d.name in si.flags.resto_lines and d.kitchen_status == "Not Sent"]
	if unsent:
		# A bill for food the kitchen never got is almost always a mistake.
		frappe.throw(
			_("Send these items to the kitchen or remove them first: {0}").format(", ".join(unsent))
		)
	payable = payable_amount(si)
	if expected is not None and abs(flt(expected) - payable) > 0.005:
		frappe.throw(
			_("The bill changed to {0} since it was shown. Check it and take the payment again.").format(
				frappe.format(payable, "Currency")
			)
		)
	_set_payments(si, payments)
	if shift and si.meta.has_field("posa_pos_opening_shift"):
		si.posa_pos_opening_shift = shift

	si.flags.ignore_permissions = True
	si.insert()
	# Hooks of other apps run on save (POSNext rewrites tax rows); the guest paid the bill shown,
	# so the saved invoice must be exactly that bill.
	if abs(payable_amount(si) - payable) > 0.005:
		frappe.throw(
			_("The saved invoice total {0} differs from the bill {1}; nothing was billed. Run the Setup Check in Resto Settings.").format(
				frappe.format(payable_amount(si), "Currency"), frappe.format(payable, "Currency")
			)
		)
	si.submit()

	billed = si.flags.resto_lines
	for row in order.items:
		if row.name in billed:
			row.billed_qty = flt(row.billed_qty) + flt(billed[row.name])
	order.append(
		"invoices",
		{
			"sales_invoice": si.name,
			"grand_total": si.grand_total,
			"paid_by": frappe.session.user,
			"lines": json.dumps(billed),
			"request_id": request_id,
		},
	)
	if not order.unbilled_lines():
		order.status = "Paid"
		order.closed_at = now_datetime()
		order.rebill = 0
	order.flags.ignore_permissions = True
	order.flags.resto_api = True
	order.flags.resto_billing = True
	order.save()
	publish("bill", order=order.name, table=order.resto_table)
	# Only a newly paid invoice prints; a retried click (request_id) returned earlier.
	from resto.resto import printing

	printing.queue_receipt(si.name, order.pos_profile)
	return si


def _set_payments(si, payments):
	if isinstance(payments, str):
		payments = json.loads(payments)
	amounts = {}
	for p in payments or []:
		amount = flt(p.get("amount"))
		if amount < 0:
			frappe.throw(_("Payment amounts cannot be negative"))
		if amount:
			amounts[p.get("mode_of_payment")] = amounts.get(p.get("mode_of_payment"), 0) + amount

	known = {p.mode_of_payment for p in si.payments}
	for mode in amounts:
		if mode not in known:
			frappe.throw(
				_("Payment method {0} is not in POS Profile {1}").format(frappe.bold(mode), frappe.bold(si.pos_profile))
			)
	for p in si.payments:
		p.amount = flt(amounts.get(p.mode_of_payment), p.precision("amount"))
		p.base_amount = flt(p.amount * flt(si.conversion_rate), p.precision("base_amount"))

	payable = payable_amount(si)
	paid = sum(flt(p.amount) for p in si.payments)
	if paid + 0.005 < payable:
		frappe.throw(
			_("Paid {0} is less than the bill {1}").format(
				frappe.format(paid, "Currency"), frappe.format(payable, "Currency")
			)
		)
	non_cash = sum(flt(p.amount) for p in si.payments if p.type != "Cash")
	if non_cash > payable + 0.005:
		frappe.throw(_("Card and bank payments cannot be more than the bill; only cash can give change"))
	si.calculate_taxes_and_totals()


# --------------------------------------------------------------------------- invoice cancel


def before_sales_invoice_cancel(doc, method=None):
	"""Cancelling a paid floor bill changes the shift cash; only a manager may do it."""
	if doc.get("resto_order") and not has_role():
		frappe.throw(_("Only a Resto Manager can cancel a restaurant bill"), frappe.PermissionError)


def validate_sales_invoice(doc, method=None):
	"""An amended copy of a floor bill would be paid twice: the order does not know it."""
	if doc.get("resto_order") and doc.get("amended_from") and doc.docstatus == 0:
		frappe.throw(_("A restaurant bill cannot be amended; bill the order again from the floor screen"))
	# After ERPNext packed the bundles: the sides actually served (and their returns).
	sides.apply_to_invoice(doc)


def before_sales_invoice_submit(doc, method=None):
	sides.check_invoice(doc)


def validate_closing_shift(doc, method=None):
	"""POSNext lists the shift's invoices when its closing dialog opens. A bill settled on the floor
	after that would be in no closing at all, so the closing must be opened again."""
	shift = doc.get("pos_opening_shift")
	if not shift or not frappe.get_meta("Sales Invoice").has_field("posa_pos_opening_shift"):
		return
	listed = {d.get("sales_invoice") for d in doc.get("pos_transactions") or []}
	floor = frappe.get_all(
		"Sales Invoice",
		filters={"posa_pos_opening_shift": shift, "docstatus": 1, "resto_order": ("is", "set")},
		pluck="name",
	)
	missing = [n for n in floor if n not in listed]
	if missing:
		frappe.throw(
			_("Restaurant bills settled after this closing was opened are missing from it: {0}. Close it and open the shift closing again.").format(
				", ".join(missing)
			)
		)


def on_sales_invoice_cancel(doc, method=None):
	"""A cancelled Resto invoice re-opens its lines on the order so they can be billed again."""
	if not doc.get("resto_order") or not frappe.db.exists("Resto Order", doc.resto_order):
		return
	order = frappe.get_doc("Resto Order", doc.resto_order)
	row = next((d for d in order.invoices if d.sales_invoice == doc.name and not d.cancelled), None)
	if not row:
		return
	billed = json.loads(row.lines or "{}")
	for line in order.items:
		if line.name in billed:
			line.billed_qty = max(flt(line.billed_qty) - flt(billed[line.name]), 0)
	row.cancelled = 1
	if order.status == "Paid":
		order.status = "Open"
		order.closed_at = None
		if order.resto_table:
			# The table may have new guests by now: the order is listed apart to be billed again.
			order.rebill = 1
	order.flags.ignore_permissions = True
	order.flags.resto_api = True
	order.flags.resto_billing = True
	order.flags.resto_allow_closed = True
	order.add_comment("Info", _("Invoice {0} was cancelled; its items can be billed again").format(doc.name))
	order.save()
