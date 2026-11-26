"""Turning a Resto Order into a standard POS Sales Invoice.

The invoice is built in memory with ERPNext's own code (POS Profile fields, payments, taxes,
discount, totals), so the bill preview and the final invoice always agree. No ERPNext or POSNext
class is overridden.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from resto.resto import packaging, sides
from resto.resto.utils import CASHIER, get_settings, has_role, publish, require_role

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
	if cint(order.get("staff_meal")):
		# Taken from stock at cost, with no sale (D-231).
		frappe.throw(_("A staff meal has no bill: send and post it instead"))
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
		# The sides changed from the recipe, for the copies of the bill and receipt that show them
		# (Resto Print Copy "Show Side Dishes"); with the order tick they are in the description.
		si.items[-1].resto_sides_text = row.get("sides_text") or None
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
		# of a choice shared by several dishes are one line. Extras given free (D-253) are their own
		# line at no price: the stock is issued all the same, and the value is kept for the report.
		free = cint(row.get("sides_free"))
		for e in json.loads(row.get("sides_extras") or "{}").values():
			extra_qty = flt(e.get("portions")) * flt(qty)
			if extra_qty <= EPS or not flt(e.get("price")):
				continue
			x = extras.setdefault(
				(e["item"], flt(e["price"]), e.get("set") or row.name, free),
				{"qty": 0, "name": e.get("name") or e["item"], "dishes": [], "by": row.get("sides_free_by")},
			)
			x["qty"] += extra_qty
			if row.item_name not in x["dishes"]:
				x["dishes"].append(row.item_name)
	for (item_code, price, _key, free), x in extras.items():
		qty = flt(x["qty"], 6)
		sides.check_extra_item(item_code)
		if sides._whole(item_code) and abs(qty - round(qty)) > 1e-6:
			frappe.throw(
				_("{0}: the extra {1} is counted in whole units, so bill all of these dishes together").format(
					frappe.bold(sides._sep().join(x["dishes"])), frappe.bold(x["name"])
				)
			)
		description = sides._t("{0} extra - {1}", x["name"], sides._sep().join(x["dishes"]))
		if free:
			description += " - " + sides._t("Free of charge")
		_append_item(si, item_code, qty, 0 if free else price, description)
		if free:
			si.items[-1].resto_free_extra = 1
			si.items[-1].resto_free_value = flt(qty * price, si.items[-1].precision("amount"))
			si.items[-1].resto_free_by = x["by"]
		prices.append(0 if free else price)
		food_total += 0 if free else qty * price

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

	# Packaging and automatic consumables (D-217, D-224): stock lines at no price, so the bill's
	# cost of goods sold includes them; each on the order types ticked for it.
	for item_code, qty, kind in packaging.lines(order, to_bill, active_invoices):
		_append_item(si, item_code, qty, 0, sides._t("Packaging" if kind == packaging.DISH else "Consumables"))
		si.items[-1].resto_packaging = kind
		prices.append(0)

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


def _set_tax_inclusive(si, profile):
	"""Whether prices include tax: the tax template decides (D-203, POSNext no longer used)."""
	return any(cint(t.included_in_print_rate) for t in si.taxes)


def payable_amount(si):
	return flt(si.grand_total) if cint(si.disable_rounded_total) else flt(si.rounded_total or si.grand_total)


# --------------------------------------------------------------------------- preview


def on_account_customer(order):
	"""The customer the rest of a bill may stay owed by (D-202), or None: only when the POS Profile
	allows partial payment, and only a named customer (never the profile's walk-in customer)."""
	profile = frappe.get_cached_doc("POS Profile", order.pos_profile)
	if not cint(profile.get("allow_partial_payment")):
		return None
	if not order.customer or order.customer == profile.customer:
		return None
	return order.customer


def on_account_needs_approval(order):
	"""A cashier leaves a bill on the account of a customer the manager gave no credit limit only with
	a manager's PIN (D-204): otherwise cash taken could be booked as owed and the drawer still balance."""
	if has_role():
		return False
	from erpnext.selling.doctype.customer.customer import get_credit_limit

	customer = on_account_customer(order)
	return bool(customer) and flt(get_credit_limit(customer, order.company)) <= 0


def check_on_account(order, approver=None, pin=None, owed=None):
	profile = frappe.get_cached_doc("POS Profile", order.pos_profile)
	if not cint(profile.get("allow_partial_payment")):
		frappe.throw(_("POS Profile {0} does not allow partial payment").format(frappe.bold(order.pos_profile)))
	customer = on_account_customer(order)
	if not customer:
		frappe.throw(_("Choose the customer who owes the rest first (Guests and Customer): not the walk-in customer"))
	# A customer with a credit limit: ERPNext checks the limit itself on submit. Without one: a
	# manager's PIN, for this customer and this amount.
	if on_account_needs_approval(order):
		from resto.resto import approval

		approval.approve(
			"on_account",
			f"{order.name}: {frappe.db.get_value('Customer', customer, 'customer_name') or customer}",
			flt(owed),
			approver,
			pin,
		)


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
				"sides_text": d.get("resto_sides_text") or "",
				"qty": flt(d.qty),
				"rate": flt(d.rate),
				"amount": flt(d.qty) * flt(d.rate),
			}
			for d in si.items
			if not d.get("resto_packaging")  # no price, not on the guest's bill (D-217)
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
		# Shown before the payment is taken; the bill itself is refused at settlement (D-224).
		"stock_warning": packaging.check_stock(si, throw=False),
		"currency": si.currency,
		"allow_partial_payment": cint(frappe.get_cached_value("POS Profile", order.pos_profile, "allow_partial_payment")),
		"on_account_customer": on_account_customer(order),
		"on_account_needs_approval": on_account_needs_approval(order) if on_account_customer(order) else False,
		# The debtor's own name, as the receipt prints it (the order's typed name may differ).
		"on_account_customer_name": frappe.db.get_value("Customer", on_account_customer(order), "customer_name")
		if on_account_customer(order)
		else None,
		"payment_modes": [
			{"mode_of_payment": p.mode_of_payment, "type": p.type, "default": cint(p.default)}
			for p in si.payments
		],
	}


# --------------------------------------------------------------------------- settle


def get_open_shift(user, pos_profile, lock=False):
	"""The user's open Resto Shift on this POS Profile (D-203), or None."""
	from resto.resto.shift import open_shift_of

	return open_shift_of(user, pos_profile, lock=lock)


def _sent_total(payments):
	if isinstance(payments, str):
		payments = json.loads(payments)
	return sum(flt(p.get("amount")) for p in payments or [])


def _by_mode(payments):
	"""{mode: amount} of a payment list (sent, or an invoice's rows), zero amounts left out."""
	if isinstance(payments, str):
		payments = json.loads(payments)
	out = {}
	for p in payments or []:
		mode, amount = p.get("mode_of_payment"), flt(p.get("amount"))
		if amount:
			out[mode] = out.get(mode, 0) + amount
	return out


def settle(order, payments, lines=None, expected=None, request_id=None, on_account=0, approver=None, pin=None):
	"""Create and submit the POS Sales Invoice, then mark the billed lines.

	expected: the amount the cashier saw; if the bill changed meanwhile (another device voided an
	item or gave a discount) nothing is billed. request_id: a retry of the same click returns the
	invoice already made instead of billing twice. on_account: what is not paid stays owed by the
	order's customer (D-202); without it the payments must cover the bill.
	"""
	require_role(CASHIER)
	settings = get_settings()
	if request_id:
		done = next((d for d in order.invoices if d.request_id == request_id and not d.cancelled), None)
		if done:
			si = frappe.get_doc("Sales Invoice", done.sales_invoice)
			# The same click again, after a lost answer: the same amounts on the same methods, or it is
			# not the same click (a card taken where the invoice says cash would go unseen).
			sent, kept = _by_mode(payments), _by_mode([p.as_dict() for p in si.payments])
			if payments and (
				set(sent) != set(kept) or any(abs(sent[m] - kept[m]) > 0.005 for m in sent)
			):
				frappe.throw(
					_("This bill was already settled as invoice {0} with {1} paid; check it before taking another payment").format(
						si.name, frappe.format(si.paid_amount, "Currency")
					)
				)
			return si
	if order.status not in ("Open", "Bill Requested"):
		frappe.throw(_("Order {0} is {1}").format(order.name, _(order.status, context="Resto")))
	# Locked: a shift being closed now finishes first (its closing counts what was billed on it),
	# and a bill after that sees it closed (a locking read sees the latest commit).
	shift = get_open_shift(frappe.session.user, order.pos_profile, lock=True)
	if cint(settings.require_pos_shift) and not shift:
		frappe.throw(
			_("Open your shift (Open Shift at the top of the floor screen) for POS Profile {0} before settling bills").format(
				frappe.bold(order.pos_profile)
			)
		)

	si = build_invoice(order, lines)
	# A packaging or consumable short in stock is named now, before the payment (D-224).
	packaging.check_stock(si)
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
	if cint(on_account):
		# After the bill is known to be the one shown: the amount left owed is the one approved.
		check_on_account(order, approver, pin, max(payable - _sent_total(payments), 0))
	_set_payments(si, payments, on_account)
	si.resto_shift = shift
	si.flags.resto_shift_set = True

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


def _set_payments(si, payments, on_account=0):
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
	if paid + 0.005 < payable and cint(on_account):
		# The rest stays on the customer's account: the invoice's outstanding amount, a receivable.
		# Nothing of it is written off, and no change is given.
		si.write_off_outstanding_amount_automatically = 0
		si.write_off_amount = 0
		si.base_write_off_amount = 0
	elif paid + 0.005 < payable:
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
	"""Cancelling a paid floor bill changes the shift cash; only a manager may do it, and not after
	its shift was closed."""
	from resto.resto.shift import check_cancel

	check_cancel(doc)
	if doc.get("resto_order") and not has_role():
		frappe.throw(_("Only a Resto Manager can cancel a restaurant bill"), frappe.PermissionError)


def validate_sales_invoice(doc, method=None):
	# The shift is set by settling (or on submit, below): never typed or sent in by hand.
	if doc.get("resto_shift") and not doc.flags.resto_shift_set and doc.docstatus != 2:
		doc.resto_shift = None
	"""An amended copy of a floor bill would be paid twice: the order does not know it."""
	if doc.get("resto_order") and doc.get("amended_from") and doc.docstatus == 0:
		frappe.throw(_("A restaurant bill cannot be amended; bill the order again from the floor screen"))
	# After ERPNext packed the bundles: the sides actually served (and their returns).
	sides.apply_to_invoice(doc)


def before_sales_invoice_submit(doc, method=None):
	sides.check_invoice(doc)
	if cint(doc.is_pos) and not doc.get("resto_shift") and doc.pos_profile and doc.meta.has_field("resto_shift"):
		doc.resto_shift = _shift_for_desk_bill(doc)


def _shift_for_desk_bill(doc):
	"""The drawer a desk POS bill's money goes into or out of. A return refunds from the shift of the
	bill it returns while that shift is open; otherwise the submitter's own open shift. Cash refunded
	with no open shift to count it is refused when shifts are required."""
	from resto.resto.shift import shift_status

	if cint(doc.is_return) and doc.return_against:
		orig = frappe.db.get_value("Sales Invoice", doc.return_against, "resto_shift")
		if orig and shift_status(orig, lock=True) == "Open":
			return orig
	shift = get_open_shift(frappe.session.user, doc.pos_profile, lock=True)
	if not shift and cint(doc.is_return) and flt(doc.paid_amount) and cint(get_settings().require_pos_shift):
		frappe.throw(_("Open your shift on the floor screen first: the refund is paid from your drawer"))
	return shift


def on_sales_invoice_cancel(doc, method=None):
	"""A cancelled Resto invoice re-opens its lines on the order so they can be billed again."""
	if not doc.get("resto_order") or not frappe.db.exists("Resto Order", doc.resto_order):
		return
	order = frappe.get_doc("Resto Order", doc.resto_order, for_update=True)
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
