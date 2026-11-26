"""The cashier's shift, kept by Resto (D-203): opened and closed from the floor screen.

A shift belongs to one cashier on the restaurant's POS Profile, and it may run past midnight. A
bill settled on the floor is linked to the cashier's open shift (Sales Invoice.resto_shift); the
closing counts what those invoices received, per payment method, from their own payment rows.
Drawer expenses are Journal Entries from the payment method's account; cash purchases from the
drawer are paid Purchase Invoices into the kitchen stock (D-225). Only the counted amounts
come from the browser: everything expected is worked out again on the server when the shift closes.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from resto.resto import approval
from resto.resto.utils import CASHIER, MANAGER, get_settings, has_role, publish, require_role

ACCOUNTS_PRECISION = 2


def _profile():
	profile = get_settings().pos_profile
	if not profile:
		frappe.throw(_("Set the POS Profile in Restaurant Settings"))
	return profile


def _modes(profile):
	"""The profile's payment methods, in its order."""
	return frappe.get_all(
		"POS Payment Method",
		filters={"parent": profile, "parenttype": "POS Profile"},
		fields=["mode_of_payment", "default"],
		order_by="idx",
	)


def _blind():
	"""The cashier counts the drawer without seeing what is expected (Restaurant Settings)."""
	return bool(cint(get_settings().get("blind_shift_count")))


def open_shift_of(user, pos_profile=None, lock=False):
	"""The user's open shift (on that POS Profile, when given), or None. lock: a locking read (it
	sees what other requests committed, and waits for a close or a bill running now)."""
	cond = " and pos_profile = %(pos_profile)s" if pos_profile else ""
	suffix = " for update" if lock else ""
	row = frappe.db.sql(
		f"""select name from `tabResto Shift` where user = %(user)s and status = 'Open'{cond}
		order by opening_time desc limit 1{suffix}""",
		{"user": user, "pos_profile": pos_profile},
	)
	return row[0][0] if row else None


def shift_status(name, lock=False):
	if not name:
		return None
	suffix = " for update" if lock else ""
	row = frappe.db.sql(f"select status from `tabResto Shift` where name = %s{suffix}", name)
	return row[0][0] if row else None


def _fresh():
	"""Start from a new transaction, so what is read after the locks below includes every bill
	committed before them (a REPEATABLE READ snapshot taken earlier in this request would not)."""
	if not frappe.flags.in_test:
		frappe.db.commit()


def _lock_user():
	"""One shift action of a user at a time (a double tap, two devices)."""
	frappe.db.sql("select name from `tabUser` where name = %s for update", frappe.session.user)


def _own_shift(lock=False):
	if lock:
		_fresh()
		_lock_user()
	name = open_shift_of(frappe.session.user, lock=lock)
	if not name:
		frappe.throw(_("You have no open shift"))
	return frappe.get_doc("Resto Shift", name)


def _mode_account(mode, company):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

	account = (get_bank_cash_account(mode, company) or {}).get("account")
	if not account:
		frappe.throw(_("Set the account of payment method {0} for company {1}").format(frappe.bold(mode), company))
	return account


# --------------------------------------------------------------------------- figures


def _cash_mode(modes):
	return next((m for m in modes if frappe.get_cached_value("Mode of Payment", m, "type") == "Cash"), None)


def _payable(si):
	return flt(si.grand_total) if cint(si.disable_rounded_total) else flt(si.rounded_total or si.grand_total)


def compute(shift):
	"""The shift's figures now: per payment method opening, received, change, expenses and
	expected; the invoices, taxes and drawer expenses; and the totals."""
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"resto_shift": shift.name, "docstatus": 1},
		fields=[
			"name", "posting_date", "customer", "is_return", "grand_total", "rounded_total",
			"disable_rounded_total", "paid_amount", "change_amount", "write_off_amount", "account_for_change_amount",
			"total_qty",
		],
		order_by="creation",
	)
	names = [i.name for i in invoices]
	pays = (
		frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ("in", names), "parenttype": "Sales Invoice"},
			fields=["parent", "mode_of_payment", "amount", "account", "type"],
		)
		if names
		else []
	)
	opening = {p.mode_of_payment: flt(p.opening_amount) for p in shift.payments}
	modes = list(opening)
	for p in pays:
		if p.mode_of_payment not in opening:
			modes.append(p.mode_of_payment)
			opening[p.mode_of_payment] = 0
	received = {m: 0.0 for m in modes}
	change = {m: 0.0 for m in modes}
	for p in pays:
		received[p.mode_of_payment] += flt(p.amount)
	by_invoice = {}
	for p in pays:
		by_invoice.setdefault(p.parent, []).append(p)
	for i in invoices:
		if flt(i.change_amount):
			# Change is paid from the change account's method (cash), as ERPNext posts it.
			rows = by_invoice.get(i.name) or []
			row = next((p for p in rows if p.account == i.account_for_change_amount), None) or next(
				(p for p in rows if p.type == "Cash"), None
			)
			# ERPNext drops a zero cash row on submit: the change still left the cash drawer.
			mode = row.mode_of_payment if row else _cash_mode(modes)
			if mode:
				change[mode] += flt(i.change_amount)

	# Debts customers paid to the cashier in this shift (Payment Entries): money in the drawer too.
	debts = frappe.get_all(
		"Payment Entry",
		filters={"resto_shift": shift.name, "docstatus": 1, "payment_type": "Receive"},
		fields=["name", "party", "party_name", "mode_of_payment", "received_amount", "posting_date"],
		order_by="creation",
	)
	for p in debts:
		if p.mode_of_payment not in opening:
			modes.append(p.mode_of_payment)
			opening[p.mode_of_payment] = received[p.mode_of_payment] = change[p.mode_of_payment] = 0
		received[p.mode_of_payment] += flt(p.received_amount)

	expenses = [
		e
		for e in shift.expenses
		if e.journal_entry and frappe.db.get_value("Journal Entry", e.journal_entry, "docstatus") == 1
	]
	spent = {m: 0.0 for m in modes}
	for e in expenses:
		spent.setdefault(e.mode_of_payment, 0.0)
		spent[e.mode_of_payment] += flt(e.amount)
		if e.mode_of_payment not in opening:
			modes.append(e.mode_of_payment)
			opening[e.mode_of_payment] = received[e.mode_of_payment] = change[e.mode_of_payment] = 0

	# Cash purchases from the drawer (D-225): paid Purchase Invoices still in force.
	purchases = _live_purchases(shift)
	bought = {m: 0.0 for m in modes}
	for p in purchases:
		bought.setdefault(p.mode_of_payment, 0.0)
		bought[p.mode_of_payment] += flt(p.amount)
		if p.mode_of_payment not in opening:
			modes.append(p.mode_of_payment)
			opening[p.mode_of_payment] = received[p.mode_of_payment] = change[p.mode_of_payment] = spent[p.mode_of_payment] = 0

	rows = []
	for m in modes:
		expected = flt(opening[m] + received[m] - change[m] - spent.get(m, 0) - bought.get(m, 0), ACCOUNTS_PRECISION)
		rows.append(
			{
				"mode_of_payment": m,
				"opening_amount": flt(opening[m], ACCOUNTS_PRECISION),
				"received_amount": flt(received[m], ACCOUNTS_PRECISION),
				"change_amount": flt(change[m], ACCOUNTS_PRECISION),
				"expenses_amount": flt(spent.get(m, 0), ACCOUNTS_PRECISION),
				"purchases_amount": flt(bought.get(m, 0), ACCOUNTS_PRECISION),
				"expected_amount": expected,
			}
		)

	inv_rows = []
	for i in invoices:
		payable = _payable(i)
		paid = flt(i.paid_amount) - flt(i.change_amount)
		inv_rows.append(
			{
				"sales_invoice": i.name,
				"posting_date": i.posting_date,
				"customer": i.customer,
				"is_return": cint(i.is_return),
				"grand_total": payable,
				"paid_amount": flt(paid, ACCOUNTS_PRECISION),
				# What the bill left owed when it was settled (D-202), rounding as the bill.
				"on_account": flt(max(payable - paid - flt(i.write_off_amount), 0) if not i.is_return else 0, ACCOUNTS_PRECISION),
			}
		)

	taxes = (
		frappe.db.sql(
			"""select account_head, max(rate) as rate, sum(base_tax_amount_after_discount_amount) as amount
			from `tabSales Taxes and Charges`
			where parenttype = 'Sales Invoice' and parent in %s
			group by account_head order by min(idx)""",
			(names,),
			as_dict=True,
		)
		if names
		else []
	)

	sales = [r for r in inv_rows if not r["is_return"]]
	returns = [r for r in inv_rows if r["is_return"]]
	gross = sum(r["grand_total"] for r in sales)
	returns_total = -sum(r["grand_total"] for r in returns)
	debts_total = sum(flt(p.received_amount) for p in debts)
	collected = sum(r["paid_amount"] for r in inv_rows) + debts_total
	expenses_total = sum(flt(e.amount) for e in expenses)
	purchases_total = sum(flt(p.amount) for p in purchases)
	# Staff meals (D-231): taken from stock at cost, no money in or out of the drawer.
	from resto.resto import staff_meal

	meals = staff_meal.of_shift(shift.name)
	return {
		"rows": rows,
		"invoices": inv_rows,
		"taxes": [{"account_head": t.account_head, "rate": flt(t.rate), "amount": flt(t.amount, ACCOUNTS_PRECISION)} for t in taxes],
		"expenses": [
			{"mode_of_payment": e.mode_of_payment, "expense_account": e.expense_account, "amount": flt(e.amount), "remarks": e.remarks, "journal_entry": e.journal_entry}
			for e in expenses
		],
		"purchases": [
			{
				"mode_of_payment": p.mode_of_payment,
				"supplier": p.supplier,
				"supplier_name": frappe.get_cached_value("Supplier", p.supplier, "supplier_name") if p.supplier else None,
				"items_text": p.items_text,
				"remarks": p.remarks,
				"amount": flt(p.amount),
				"purchase_invoice": p.purchase_invoice,
			}
			for p in purchases
		],
		"debts": [
			{"payment_entry": p.name, "customer": p.party_name or p.party, "mode_of_payment": p.mode_of_payment, "amount": flt(p.received_amount)}
			for p in debts
		],
		"staff_meals": [{"order": m.name, "order_no": m.order_no, "staff_for": m.staff_for, "cost": flt(m.staff_cost)} for m in meals],
		"summary": {
			"debts_count": len(debts),
			"debts_total": flt(debts_total, ACCOUNTS_PRECISION),
			"sales_count": len(sales),
			"gross_sales": flt(gross, ACCOUNTS_PRECISION),
			"returns_count": len(returns),
			"returns_total": flt(returns_total, ACCOUNTS_PRECISION),
			"net_sales": flt(gross - returns_total, ACCOUNTS_PRECISION),
			"collected": flt(collected, ACCOUNTS_PRECISION),
			"outstanding": flt(sum(r["on_account"] for r in inv_rows), ACCOUNTS_PRECISION),
			"expenses_count": len(expenses),
			"expenses_total": flt(expenses_total, ACCOUNTS_PRECISION),
			"purchases_count": len(purchases),
			"purchases_total": flt(purchases_total, ACCOUNTS_PRECISION),
			"net_cash": flt(collected - expenses_total - purchases_total, ACCOUNTS_PRECISION),
			"tax_total": flt(sum(flt(t.amount) for t in taxes), ACCOUNTS_PRECISION),
			"total_quantity": flt(sum(flt(i.total_qty) for i in invoices)),
			"staff_meals_count": len(meals),
			"staff_meals_cost": flt(sum(flt(m.staff_cost) for m in meals), ACCOUNTS_PRECISION),
		},
	}


def _report(shift, figures):
	"""The shape the floor's shift dialog shows."""
	return {
		"shift": shift.name,
		"pos_profile": shift.pos_profile,
		"user": shift.user,
		"period_start": shift.opening_time,
		"period_end": shift.closing_time,
		"summary": figures["summary"],
		"taxes": figures["taxes"],
		"invoices": [
			{
				"name": r["sales_invoice"],
				"posting_date": r["posting_date"],
				"customer": r["customer"],
				"grand_total": r["grand_total"],
				"is_return": r["is_return"],
				"outstanding_amount": r["on_account"],
			}
			for r in figures["invoices"]
		],
		"expenses": figures["expenses"],
		"purchases": figures["purchases"],
		"debts": figures["debts"],
		"staff_meals": figures["staff_meals"],
		"rows": figures["rows"],
	}


# --------------------------------------------------------------------------- floor API


def status():
	"""The cashier's shift, for the floor screen (cashiers only)."""
	settings = get_settings()
	name = open_shift_of(frappe.session.user)
	out = {"required": cint(settings.require_pos_shift), "pos_profile": settings.pos_profile, "shift": name}
	if name:
		since, profile = frappe.db.get_value("Resto Shift", name, ["opening_time", "pos_profile"])
		out["since"] = since
		# Open on a POS Profile the restaurant no longer bills through: closed first.
		out["other_profile"] = profile if profile != settings.pos_profile else None
	return out


def ordering_allowed(user=None):
	"""(allowed, reason): with "Require an Open Shift" on, no order is started, added to, sent or
	billed without a shift (D-211). A cashier or a manager (Resto Cashier or Resto Manager role) needs
	their own open shift; a waiter or host, any open shift on the restaurant's POS Profile."""
	settings = get_settings()
	if not cint(settings.require_pos_shift):
		return True, None
	user = user or frappe.session.user
	if {CASHIER, MANAGER} & set(frappe.get_roles(user)):
		if open_shift_of(user, settings.pos_profile):
			return True, None
		return False, _("Open your shift first (Open Shift at the top of the floor screen)")
	if frappe.db.exists("Resto Shift", {"status": "Open", "pos_profile": settings.pos_profile}):
		return True, None
	return False, _("No cashier shift is open: orders start once a cashier opens a shift")


def require_ordering():
	allowed, reason = ordering_allowed()
	if not allowed:
		frappe.throw(reason, title=_("No Open Shift"))


@frappe.whitelist()
def opening_data():
	"""What the opening shows: the restaurant's POS Profile, its company, warehouse and currency,
	the cashier, now, and each payment method for its opening balance."""
	require_role(CASHIER)
	profile = _profile()
	p = frappe.db.get_value("POS Profile", profile, ["company", "warehouse", "currency"], as_dict=True) or {}
	return {
		"pos_profile": profile,
		"company": p.get("company"),
		"warehouse": p.get("warehouse"),
		"currency": p.get("currency"),
		"cashier": frappe.utils.get_fullname(frappe.session.user),
		"now": str(now_datetime()),
		"open_shift": open_shift_of(frappe.session.user),
		"modes": [m.mode_of_payment for m in _modes(profile)],
	}


@frappe.whitelist(methods=["POST"])
def open_shift(amounts=None):
	"""Open the cashier's shift on the restaurant's POS Profile, with the money in the drawer."""
	require_role(CASHIER)
	profile = _profile()
	_fresh()
	_lock_user()
	already = open_shift_of(frappe.session.user, lock=True)
	if already:
		frappe.throw(_("You already have an open shift: {0}").format(already))
	users = frappe.get_all("POS Profile User", {"parent": profile, "parenttype": "POS Profile"}, pluck="user")
	if users and frappe.session.user not in users:
		frappe.throw(_("You are not a user of POS Profile {0}; ask the manager to add you").format(frappe.bold(profile)))
	p = frappe.db.get_value("POS Profile", profile, ["company", "currency", "disabled"], as_dict=True)
	if cint(p.disabled):
		frappe.throw(_("POS Profile {0} is disabled").format(frappe.bold(profile)))
	amounts = json.loads(amounts) if isinstance(amounts, str) else (amounts or {})
	modes = _modes(profile)
	if not modes:
		frappe.throw(_("No payment methods configured for this POS Profile"))
	shift = frappe.new_doc("Resto Shift")
	shift.update(
		{
			"user": frappe.session.user,
			"pos_profile": profile,
			"company": p.company,
			"currency": p.currency,
			"status": "Open",
			"opening_time": now_datetime(),
		}
	)
	for m in modes:
		amount = flt(amounts.get(m.mode_of_payment), ACCOUNTS_PRECISION)
		if amount < 0:
			frappe.throw(_("{0}: the opening amount cannot be negative").format(m.mode_of_payment))
		shift.append("payments", {"mode_of_payment": m.mode_of_payment, "opening_amount": amount, "currency": p.currency})
	shift.flags.resto_shift = True
	shift.insert(ignore_permissions=True)
	publish("shift")  # waiters' screens may take orders now (D-211)
	return shift.name


@frappe.whitelist()
def closing_data():
	"""What the close dialog shows. Counting blind (Restaurant Settings), only the payment
	methods are given until the shift is closed."""
	require_role(CASHIER)
	shift = _own_shift()
	report = _report(shift, compute(shift))
	report["blind"] = _blind()
	if report["blind"]:
		report = {
			"shift": report["shift"],
			"pos_profile": report["pos_profile"],
			"blind": True,
			"period_start": report["period_start"],
			"rows": [{"mode_of_payment": r["mode_of_payment"]} for r in report["rows"]],
		}
	# Orders still open on this profile: their bills are not in this shift.
	from resto.resto.doctype.resto_order.resto_order import ACTIVE

	report["open_orders"] = frappe.db.count("Resto Order", {"status": ("in", ACTIVE), "pos_profile": shift.pos_profile})
	return report


@frappe.whitelist(methods=["POST"])
def close_shift(amounts):
	"""Close the cashier's shift with the amounts counted. A bill being settled on it finishes
	first (it holds the shift row), and none is linked to it after."""
	require_role(CASHIER)
	shift = _own_shift(lock=True)
	amounts = json.loads(amounts) if isinstance(amounts, str) else (amounts or {})
	figures = compute(shift)
	for row in figures["rows"]:
		mode = row["mode_of_payment"]
		if mode not in amounts or amounts[mode] in (None, ""):
			frappe.throw(_("Enter the amount counted for {0}").format(frappe.bold(mode)))
		if flt(amounts[mode]) < 0:
			frappe.throw(_("{0}: the amount counted cannot be negative").format(mode))
		row["closing_amount"] = flt(amounts[mode], ACCOUNTS_PRECISION)
		row["difference"] = flt(row["closing_amount"] - row["expected_amount"], ACCOUNTS_PRECISION)
		row["currency"] = shift.currency
	shift.set("payments", figures["rows"])
	shift.set("invoices", [{**r, "currency": shift.currency} for r in figures["invoices"]])
	shift.set("taxes", [{**t, "currency": shift.currency} for t in figures["taxes"]])
	s = figures["summary"]
	shift.update(
		{
			"status": "Closed",
			"closing_time": now_datetime(),
			"sales_count": s["sales_count"],
			"gross_sales": s["gross_sales"],
			"returns_count": s["returns_count"],
			"returns_total": s["returns_total"],
			"net_sales": s["net_sales"],
			"collected": s["collected"],
			"on_account": s["outstanding"],
			"tax_total": s["tax_total"],
			"expenses_total": s["expenses_total"],
			"purchases_total": s["purchases_total"],
			"net_cash": s["net_cash"],
			"total_difference": flt(sum(r["difference"] for r in figures["rows"]), ACCOUNTS_PRECISION),
		}
	)
	shift.over_short_entry = _over_short_entry(shift, figures["rows"])
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	publish("shift")
	report = _report(shift, figures)
	report["closing"] = shift.name
	return report


def _set_remark(je, text):
	"""The reason of a Journal Entry, where its form shows it (D-234). v16 hides User Remark and shows
	Remark, which ERPNext rebuilds from cheque and references unless Custom Remark is ticked; the
	General Ledger's remarks are taken from it too. User Remark keeps the same text for older reads."""
	je.user_remark = text
	je.custom_remark = 1
	je.remark = text


def _cost_center(shift):
	return frappe.db.get_value("POS Profile", shift.pos_profile, "cost_center") or frappe.get_cached_value(
		"Company", shift.company, "cost_center"
	)


def _over_short_entry(shift, rows):
	"""What the cash counted differs from what was expected: with a Cash Over/Short Account (Resto
	Settings) a Journal Entry moves the difference, so the cash account holds what is in the drawer
	(D-206). Cash methods only: a card or wallet difference is a booking mistake to find, not money
	gone, and its account must keep matching the bank."""
	account = get_settings().cash_over_short_account
	if not account:
		return None
	if frappe.db.get_value("Account", account, "company") != shift.company:
		frappe.throw(_("Cash Over/Short Account {0} is not of company {1} (Resto Settings)").format(account, shift.company))
	cost_center = _cost_center(shift)
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Cash Entry",
			"company": shift.company,
			"posting_date": frappe.utils.nowdate(),
			}
	)
	_set_remark(je, _("Cash over/short, shift {0}").format(shift.name))
	for r in rows:
		diff = flt(r.get("difference"), ACCOUNTS_PRECISION)
		if abs(diff) < 0.005 or frappe.get_cached_value("Mode of Payment", r["mode_of_payment"], "type") != "Cash":
			continue
		cash = _mode_account(r["mode_of_payment"], shift.company)
		# Short (diff < 0): the loss is debited, the cash account credited; over: the other way.
		loss, gain = (account, cash) if diff < 0 else (cash, account)
		je.append("accounts", {"account": loss, "debit_in_account_currency": abs(diff), "cost_center": cost_center})
		je.append("accounts", {"account": gain, "credit_in_account_currency": abs(diff), "cost_center": cost_center})
	if not je.accounts:
		return None
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def shift_print_data(name):
	"""For the shift print format (a Jinja method): the items sold and the debts collected."""
	if not frappe.has_permission("Resto Shift", "read", name):
		return {"items": [], "debts": [], "staff_meals": {"count": 0, "cost": 0}}
	items = frappe.db.sql(
		"""select sii.item_code, sii.item_name, sum(sii.stock_qty) as qty, sum(sii.base_net_amount) as amount
		from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
		where si.resto_shift = %s and si.docstatus = 1 and ifnull(sii.resto_packaging, '') = ''
		group by sii.item_code, sii.item_name order by amount desc""",
		(name,),
		as_dict=True,
	)
	debts = frappe.get_all(
		"Payment Entry",
		filters={"resto_shift": name, "docstatus": 1, "payment_type": "Receive"},
		fields=["name", "party_name", "party", "mode_of_payment", "received_amount"],
		order_by="creation",
	)
	from resto.resto import staff_meal

	meals = staff_meal.of_shift(name)
	return {"items": items, "debts": debts, "staff_meals": {"count": len(meals), "cost": flt(sum(flt(m.staff_cost) for m in meals), ACCOUNTS_PRECISION)}}


# --------------------------------------------------------------------------- drawer expenses


@frappe.whitelist()
def expense_accounts():
	"""The expense accounts a drawer expense can go to, and the shift's payment methods."""
	require_role(CASHIER)
	shift = _own_shift()
	settings = get_settings()
	spent = _spent(shift)
	cap = flt(settings.drawer_expense_cap)
	return {
		"accounts": _expense_accounts(shift.company),
		"modes": _cash_modes(shift),
		"receipt_required": cint(settings.drawer_expense_receipt),
		# What is left of the cap this shift (None: no cap; a manager has none).
		"cap_left": None if not cap or has_role() else max(flt(cap - spent, ACCOUNTS_PRECISION), 0),
	}


def _expense_accounts(company):
	"""The accounts a cashier may pay from the drawer: the list in Resto Settings, or else any
	ordinary expense account (D-205)."""
	accounts = [
		a
		for a in frappe.get_all(
			"Account",
			filters={"company": company, "root_type": "Expense", "is_group": 0, "disabled": 0},
			fields=["name", "account_name", "account_type"],
			order_by="lft",
		)
		if (a.account_type or "") in EXPENSE_TYPES
	]
	allowed = {r.account for r in get_settings().get("drawer_expense_accounts") or []}
	return [a for a in accounts if a.name in allowed] if allowed else accounts


def _spent(shift):
	"""Paid out of the drawer so far in this shift (entries still in force)."""
	names = [e.journal_entry for e in shift.expenses if e.journal_entry]
	if not names:
		return 0
	live = set(frappe.get_all("Journal Entry", filters={"name": ("in", names), "docstatus": 1}, pluck="name"))
	return flt(sum(flt(e.amount) for e in shift.expenses if e.journal_entry in live), ACCOUNTS_PRECISION)


def _receipt_file(file_url):
	"""The receipt the cashier just uploaded (theirs, not yet attached to anything), not already the
	receipt of another expense or cash purchase."""
	name = frappe.db.get_value(
		"File",
		{"file_url": file_url, "owner": frappe.session.user, "attached_to_name": ("is", "not set"), "is_folder": 0},
		"name",
	)
	if not name:
		frappe.throw(_("The receipt was not found: attach it again"))
	content_hash = frappe.db.get_value("File", name, "content_hash")
	if content_hash and frappe.db.exists(
		"File",
		{"content_hash": content_hash, "attached_to_doctype": ("in", ("Journal Entry", "Purchase Invoice")), "name": ("!=", name)},
	):
		frappe.throw(_("This receipt is already attached to another expense"))
	return name


# Ordinary expenses only: not cost of goods sold, stock adjustment, depreciation and the like, which
# the stock and the food cost reports own.
EXPENSE_TYPES = ("", "Expense Account", "Chargeable", "Indirect Expense")


def _cash_modes(shift):
	"""Only cash leaves a drawer: an expense "paid by card" would credit the bank with no bank movement."""
	return [
		p.mode_of_payment
		for p in shift.payments
		if frappe.get_cached_value("Mode of Payment", p.mode_of_payment, "type") == "Cash"
	]


@frappe.whitelist(methods=["POST"])
def add_expense(mode_of_payment, expense_account, amount, remarks=None, request_id=None, receipt=None):
	"""Money paid out of the drawer: a Journal Entry from the payment method's account to the
	expense account, counted by the shift (its expected amount goes down). Only the accounts allowed
	in Resto Settings, up to the cap per shift, with the receipt attached when required (D-205)."""
	require_role(CASHIER)
	shift = _own_shift(lock=True)
	if request_id:
		# The same click again, after a lost answer: the entry already made.
		done = next((e for e in shift.expenses if e.request_id == request_id), None)
		if done:
			return done.journal_entry
	amount = flt(amount, ACCOUNTS_PRECISION)
	if amount <= 0:
		frappe.throw(_("The expense amount must be more than zero"))
	if mode_of_payment not in _cash_modes(shift):
		frappe.throw(_("Payment method {0} is not a cash method of this shift").format(frappe.bold(mode_of_payment)))
	acc = frappe.db.get_value(
		"Account", expense_account, ["company", "root_type", "is_group", "disabled", "account_type"], as_dict=True
	)
	if (
		not acc
		or acc.company != shift.company
		or acc.root_type != "Expense"
		or cint(acc.is_group)
		or cint(acc.disabled)
		or (acc.account_type or "") not in EXPENSE_TYPES
	):
		frappe.throw(_("{0} is not an expense account of {1}").format(expense_account, shift.company))
	if expense_account not in {a.name for a in _expense_accounts(shift.company)}:
		frappe.throw(_("{0} is not one of the drawer expense accounts (Resto Settings)").format(expense_account))
	if not (remarks or "").strip():
		frappe.throw(_("Write what the expense was for"))
	settings = get_settings()
	if cint(settings.drawer_expense_receipt) and not receipt:
		frappe.throw(_("Attach the receipt of this expense"))
	receipt_file = _receipt_file(receipt) if receipt else None
	cap = flt(settings.drawer_expense_cap)
	if cap and not has_role():
		left = flt(cap - _spent(shift), ACCOUNTS_PRECISION)
		if amount > left + 0.005:
			frappe.throw(
				_("Drawer expenses are capped at {0} per shift; {1} is left: a manager pays the rest").format(
					frappe.format(cap, "Currency"), frappe.format(max(left, 0), "Currency")
				)
			)
	row = next(r for r in compute(shift)["rows"] if r["mode_of_payment"] == mode_of_payment)
	if amount > row["expected_amount"] + 0.005:
		if _blind():
			# Counting blind: the cashier is not told what the drawer should hold.
			frappe.throw(_("The drawer does not hold that much for {0}").format(mode_of_payment))
		frappe.throw(
			_("Only {0} should be in the drawer for {1}").format(frappe.format(row["expected_amount"], "Currency"), mode_of_payment)
		)
	cost_center = _cost_center(shift)
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Cash Entry",
			"company": shift.company,
			"posting_date": frappe.utils.nowdate(),
			}
	)
	_set_remark(je, _("Drawer expense, shift {0}: {1}").format(shift.name, remarks.strip()))
	je.append("accounts", {"account": expense_account, "debit_in_account_currency": amount, "cost_center": cost_center})
	je.append(
		"accounts",
		{"account": _mode_account(mode_of_payment, shift.company), "credit_in_account_currency": amount, "cost_center": cost_center},
	)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	if receipt_file:
		# On the entry itself, where the accountant looks for it.
		frappe.db.set_value("File", receipt_file, {"attached_to_doctype": "Journal Entry", "attached_to_name": je.name})
	shift.append(
		"expenses",
		{
			"mode_of_payment": mode_of_payment,
			"expense_account": expense_account,
			"amount": amount,
			"remarks": remarks.strip(),
			"journal_entry": je.name,
			"receipt": receipt or None,
			"currency": shift.currency,
			"request_id": request_id,
		},
	)
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	return je.name


# --------------------------------------------------------------------------- cash purchases (D-225)

MAX_PURCHASE_LINES = 30
UNUSUAL = 3  # a price per unit this many times above or below the usual one needs a manager


def _live_purchases(shift):
	"""The shift's cash purchases whose Purchase Invoice is still in force."""
	rows = [p for p in shift.get("purchases") or [] if p.purchase_invoice]
	if not rows:
		return []
	live = set(
		frappe.get_all("Purchase Invoice", filters={"name": ("in", [p.purchase_invoice for p in rows]), "docstatus": 1}, pluck="name")
	)
	return [p for p in rows if p.purchase_invoice in live]


def _cash_supplier():
	"""The supplier of a cash purchase when the cashier names none: Resto Settings' Cash Supplier,
	made on the first purchase when it is empty."""
	settings = get_settings()
	name = settings.get("cash_supplier")
	if name and frappe.db.exists("Supplier", name):
		return name
	from resto.resto.sides import _t

	label = _t("Cash Supplier")
	name = frappe.db.get_value("Supplier", {"supplier_name": label}, "name")
	if not name:
		supplier = frappe.new_doc("Supplier")
		supplier.supplier_name = label
		supplier.supplier_type = "Individual"
		# Bought at the market: there is never a purchase order or receipt before the invoice.
		for field in ("allow_purchase_invoice_creation_without_purchase_order", "allow_purchase_invoice_creation_without_purchase_receipt"):
			if supplier.meta.has_field(field):
				supplier.set(field, 1)
		supplier.supplier_group = frappe.db.get_single_value("Buying Settings", "supplier_group") or frappe.db.get_value(
			"Supplier Group", {"is_group": 0}, "name"
		)
		supplier.flags.ignore_permissions = True
		supplier.insert()
		name = supplier.name
	frappe.db.set_single_value("Resto Settings", "cash_supplier", name)
	frappe.clear_document_cache("Resto Settings", "Resto Settings")
	return name


def _purchase_items(items):
	"""[{item_code, qty, amount}] of a cash purchase, checked: stock items that can be bought, in
	their stock unit, each with what was paid for it."""
	from resto.resto.doctype.resto_recipe.resto_recipe import MENU, get_active_recipe

	rows = json.loads(items) if isinstance(items, str) else (items or [])
	# The invoice keeps quantities at its own precision: the drawer pays for what the stock receives.
	qty_precision = frappe.get_precision("Purchase Invoice Item", "qty") or 3
	out = []
	for r in rows:
		code = (r.get("item_code") or "").strip()
		if not code:
			continue
		item = frappe.db.get_value(
			"Item",
			code,
			["item_name", "stock_uom", "disabled", "is_stock_item", "is_purchase_item", "has_variants", "has_batch_no", "has_serial_no"],
			as_dict=True,
		)
		name = frappe.bold(item.item_name if item else code)
		if not item or item.disabled or item.has_variants or not item.is_stock_item or get_active_recipe(code, MENU):
			frappe.throw(_("{0} is not a stock item that can be bought: drawer expenses are for what is not stock").format(name))
		if not item.is_purchase_item:
			frappe.throw(_("{0} cannot be bought: tick Allow Purchase on the item").format(name))
		if item.has_batch_no or item.has_serial_no:
			frappe.throw(_("{0} has batch or serial numbers: it is received on a purchase receipt, not from the drawer").format(name))
		qty, amount = flt(r.get("qty"), qty_precision), flt(r.get("amount"), ACCOUNTS_PRECISION)
		if qty <= 0 or amount <= 0:
			frappe.throw(_("{0}: enter the quantity and the amount paid").format(name))
		same = next((o for o in out if o.item_code == code), None)
		if same:
			# One line per item (ERPNext may refuse the same item twice): the quantities and amounts add up.
			same.qty = flt(same.qty + qty, qty_precision)
			same.amount = flt(same.amount + amount, ACCOUNTS_PRECISION)
			continue
		out.append(frappe._dict(item_code=code, item_name=item.item_name, stock_uom=item.stock_uom, qty=qty, amount=amount))
	if not out:
		frappe.throw(_("Add what was bought"))
	if len(out) > MAX_PURCHASE_LINES:
		frappe.throw(_("A cash purchase has at most {0} lines").format(MAX_PURCHASE_LINES))
	return out


def _usual_rate(item_code, warehouse):
	"""What the item usually costs per stock unit: its valuation in the warehouse, else the item's."""
	rate = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"))
	if rate <= 0:
		rate = flt(frappe.db.get_value("Item", item_code, "valuation_rate")) or flt(frappe.db.get_value("Item", item_code, "last_purchase_rate"))
	return rate


def _unusual(rows, warehouse):
	"""Lines whose price per stock unit is a third or less, or three times or more, of the usual: most
	often a quantity typed in grams for a kilogram item. A manager approves them."""
	out = []
	for r in rows:
		usual = _usual_rate(r.item_code, warehouse)
		rate = r.amount / r.qty
		if usual > 0 and (rate <= usual / UNUSUAL or rate >= usual * UNUSUAL):
			out.append(
				_("{0}: {1} per {2}, usually {3}").format(
					r.item_name, frappe.format(rate, "Currency"), _(r.stock_uom), frappe.format(usual, "Currency")
				)
			)
	return out


def _up(value):
	"""Up to the next cent: the lines then add up to at least what was paid, and the invoice's
	discount takes back the cents above it."""
	import math

	return math.ceil(flt(value) * 100 - 1e-6) / 100


def _purchase_invoice(shift, rows, total, mode_of_payment, supplier, warehouse, remarks):
	"""A paid Purchase Invoice that takes the items into the kitchen at what was paid and pays it
	from the drawer's cash account: Dr stock, Cr cash, nothing owed to the supplier."""
	cost_center = _cost_center(shift)
	pi = frappe.new_doc("Purchase Invoice")
	pi.flags.ignore_permissions = True
	pi.update(
		{
			"company": shift.company,
			"supplier": supplier,
			"posting_date": frappe.utils.nowdate(),
			"posting_time": frappe.utils.nowtime(),
			"set_posting_time": 1,
			"update_stock": 1,
			"set_warehouse": warehouse,
			"is_paid": 1,
			"mode_of_payment": mode_of_payment,
			"cash_bank_account": _mode_account(mode_of_payment, shift.company),
			"cost_center": cost_center,
			"ignore_pricing_rule": 1,
			"disable_rounded_total": 1,
			"remarks": _("Cash purchase from the drawer, shift {0}").format(shift.name) + (f": {remarks}" if remarks else ""),
		}
	)
	for r in rows:
		pi.append(
			"items",
			{
				"item_code": r.item_code,
				"qty": r.qty,
				"uom": r.stock_uom,
				"stock_uom": r.stock_uom,
				"conversion_factor": 1,
				"rate": _up(r.amount / r.qty),
				"warehouse": warehouse,
				"cost_center": cost_center,
			},
		)
	pi.set_missing_values()
	# What was paid, as paid: no purchase taxes, tax withheld or price list on top of it.
	if pi.meta.has_field("apply_tds"):
		pi.apply_tds = 0
	pi.taxes_and_charges = None
	pi.set("taxes", [])
	for d, r in zip(pi.items, rows):
		d.price_list_rate = d.rate = _up(r.amount / r.qty)
		d.discount_percentage = d.discount_amount = d.margin_rate_or_amount = 0
		d.warehouse = warehouse
	pi.apply_discount_on = "Net Total"
	pi.additional_discount_percentage = 0
	pi.discount_amount = 0
	pi.calculate_taxes_and_totals()
	pi.discount_amount = flt(flt(pi.total) - total, ACCOUNTS_PRECISION)
	pi.calculate_taxes_and_totals()
	if abs(flt(pi.grand_total) - total) > 0.005:
		frappe.throw(
			_("The purchase invoice total {0} differs from the amount paid {1}; nothing was recorded").format(
				frappe.format(pi.grand_total, "Currency"), frappe.format(total, "Currency")
			)
		)
	pi.paid_amount = pi.base_paid_amount = total
	pi.insert()
	# Hooks of other apps run on save: the invoice saved must still be exactly what was paid.
	if abs(flt(pi.grand_total) - total) > 0.005 or abs(flt(pi.outstanding_amount)) > 0.005 or pi.get("taxes"):
		frappe.throw(
			_("The purchase invoice total {0} differs from the amount paid {1}; nothing was recorded").format(
				frappe.format(pi.grand_total, "Currency"), frappe.format(total, "Currency")
			)
		)
	pi.submit()
	return pi


@frappe.whitelist()
def purchase_data():
	"""What the floor's Cash Purchase dialog needs."""
	require_role(CASHIER)
	shift = _own_shift()
	settings = get_settings()
	cap = flt(settings.get("drawer_purchase_cap"))
	return {
		"modes": _cash_modes(shift),
		"suppliers": frappe.get_all(
			"Supplier", filters={"disabled": 0}, fields=["name", "supplier_name"], order_by="supplier_name", limit=500
		),
		"cash_supplier": settings.get("cash_supplier"),
		"warehouse": settings.kitchen_warehouse,
		# Above it a manager approves (None: never, or the user is a manager).
		"approval_above": None if not cap or has_role() else cap,
	}


@frappe.whitelist(methods=["POST"])
def cash_purchase(items, mode_of_payment, supplier=None, receipt=None, remarks=None, request_id=None, approver=None, pin=None):
	"""Food or other stock bought for cash out of the drawer (D-225): a paid Purchase Invoice with
	the items received into the kitchen at what was paid, paid from the shift's cash method. The
	shift's expected cash goes down by it, and its close lists it apart from the drawer expenses. Above
	Resto Settings' amount a manager approves it."""
	require_role(CASHIER)
	shift = _own_shift(lock=True)
	if request_id:
		# The same click again, after a lost answer: the purchase already made.
		done = next((p for p in shift.get("purchases") or [] if p.request_id == request_id), None)
		if done:
			return done.purchase_invoice
	settings = get_settings()
	warehouse = settings.kitchen_warehouse
	if not warehouse:
		frappe.throw(_("Set the Kitchen Warehouse in Resto Settings"))
	if mode_of_payment not in _cash_modes(shift):
		frappe.throw(_("Payment method {0} is not a cash method of this shift").format(frappe.bold(mode_of_payment)))
	rows = _purchase_items(items)
	total = flt(sum(r.amount for r in rows), ACCOUNTS_PRECISION)
	if supplier:
		if not frappe.db.exists("Supplier", {"name": supplier, "disabled": 0}):
			frappe.throw(_("Supplier {0} was not found").format(frappe.bold(supplier)))
	else:
		supplier = _cash_supplier()
	remarks = (remarks or "").strip()
	receipt_file = _receipt_file(receipt) if receipt else None
	row = next((r for r in compute(shift)["rows"] if r["mode_of_payment"] == mode_of_payment), None)
	if not row or total > row["expected_amount"] + 0.005:
		if _blind() or not row:
			frappe.throw(_("The drawer does not hold that much for {0}").format(mode_of_payment))
		frappe.throw(
			_("Only {0} should be in the drawer for {1}").format(frappe.format(row["expected_amount"], "Currency"), mode_of_payment)
		)
	items_text = ", ".join(f"{frappe.format(r.qty, 'Float')} {_(r.stock_uom)} {r.item_name}" for r in rows)
	approved_by = None
	cap = flt(settings.get("drawer_purchase_cap"))
	unusual = _unusual(rows, warehouse)
	if unusual and not has_role() and not (approver and pin):
		# Said plainly first: most often the quantity is not in the stock unit.
		frappe.throw(
			_("Check the quantities are in the stock unit: {0}. If the prices are right, a manager approves.").format("; ".join(unusual)),
			approval.ApprovalRequired,
		)
	if (cap and total > cap + 0.005) or unusual:
		approved_by = approval.approve("cash_purchase", shift.name, total, approver, pin, details="; ".join([items_text, *unusual]))
	pi = _purchase_invoice(shift, rows, total, mode_of_payment, supplier, warehouse, remarks)
	if receipt_file:
		# On the invoice itself, where the accountant looks for it.
		frappe.db.set_value("File", receipt_file, {"attached_to_doctype": "Purchase Invoice", "attached_to_name": pi.name})
	shift.append(
		"purchases",
		{
			"mode_of_payment": mode_of_payment,
			"supplier": supplier,
			"amount": total,
			"items_text": items_text,
			"remarks": remarks or None,
			"purchase_invoice": pi.name,
			"receipt": receipt or None,
			"approved_by": approved_by,
			"currency": shift.currency,
			"request_id": request_id,
		},
	)
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	return pi.name


def link_payment_entry(doc, method=None):
	"""Payment Entry before_submit: money a customer pays to a cashier who has an open shift (a debt
	left on account, D-202) is in that cashier's drawer, so the shift counts it."""
	if doc.get("resto_shift") or doc.payment_type != "Receive" or doc.party_type != "Customer" or not doc.mode_of_payment:
		return
	if not doc.meta.has_field("resto_shift"):
		return
	name = open_shift_of(frappe.session.user, lock=True)
	if name and doc.company == frappe.db.get_value("Resto Shift", name, "company"):
		doc.resto_shift = name


def check_cancel(doc, method=None):
	"""before_cancel of Sales Invoice, Payment Entry and Journal Entry: what a closed shift counted
	stays as it counted it (ERPNext does the same for its own POS closing). A credit note or a new
	entry corrects it instead."""
	name = doc.get("resto_shift")
	if not name and doc.doctype == "Purchase Invoice":
		name = frappe.db.get_value("Resto Shift Purchase", {"purchase_invoice": doc.name}, "parent")
	if not name and doc.doctype == "Journal Entry":
		name = frappe.db.get_value("Resto Shift Expense", {"journal_entry": doc.name}, "parent") or frappe.db.get_value(
			"Resto Shift", {"over_short_entry": doc.name}, "name"
		)
	if name and shift_status(name, lock=True) == "Closed":
		frappe.throw(
			_("{0} is in closed shift {1} and cannot be cancelled; make a return (credit note) or a new entry instead").format(
				doc.name, name
			)
		)


# --------------------------------------------------------------------------- debts collected


def _open_debts(customer, company):
	"""The customer's unpaid bills, oldest first."""
	return frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "company": company, "docstatus": 1, "is_return": 0, "outstanding_amount": (">", 0)},
		fields=["name", "posting_date", "outstanding_amount", "grand_total", "rounded_total"],
		order_by="posting_date asc, creation asc",
	)


@frappe.whitelist()
def customer_debt(customer):
	"""What a customer owes, bill by bill, for the floor's Collect Debt dialog."""
	require_role(CASHIER)
	shift = _own_shift()
	debts = _open_debts(customer, shift.company)
	return {
		"customer_name": frappe.db.get_value("Customer", customer, "customer_name"),
		"total": flt(sum(flt(d.outstanding_amount) for d in debts), ACCOUNTS_PRECISION),
		"invoices": [{"name": d.name, "posting_date": d.posting_date, "outstanding_amount": flt(d.outstanding_amount)} for d in debts],
		"modes": [p.mode_of_payment for p in shift.payments],
	}


@frappe.whitelist(methods=["POST"])
def collect_debt(customer, amount, mode_of_payment, request_id=None):
	"""A customer pays (part of) what they owe to the cashier: a Payment Entry against their unpaid
	bills, oldest first, linked to the cashier's shift (its drawer holds the money)."""
	require_role(CASHIER)
	shift = _own_shift(lock=True)
	if request_id:
		done = frappe.db.get_value("Payment Entry", {"resto_shift": shift.name, "reference_no": request_id, "docstatus": 1}, "name")
		if done:
			return done
	amount = flt(amount, ACCOUNTS_PRECISION)
	if amount <= 0:
		frappe.throw(_("The amount must be more than zero"))
	if mode_of_payment not in [p.mode_of_payment for p in shift.payments]:
		frappe.throw(_("Payment method {0} is not in this shift").format(frappe.bold(mode_of_payment)))
	# The customer's bills, locked: two collections of the same debt never both go through.
	debts = _open_debts(customer, shift.company)
	if debts:
		frappe.db.sql(
			"select name from `tabSales Invoice` where name in %s for update", ([d.name for d in debts],)
		)
		debts = _open_debts(customer, shift.company)
	owed = flt(sum(flt(d.outstanding_amount) for d in debts), ACCOUNTS_PRECISION)
	if amount > owed + 0.005:
		frappe.throw(_("{0} owes {1} only").format(frappe.db.get_value("Customer", customer, "customer_name"), frappe.format(owed, "Currency")))

	from erpnext.accounts.party import get_party_account

	party_account = get_party_account("Customer", customer, shift.company)
	cash_account = _mode_account(mode_of_payment, shift.company)
	acc = lambda name: frappe.db.get_value("Account", name, ["account_currency", "account_type"], as_dict=True)  # noqa: E731
	frm, to = acc(party_account), acc(cash_account)
	pe = frappe.new_doc("Payment Entry")
	pe.update(
		{
			"payment_type": "Receive",
			"company": shift.company,
			"posting_date": frappe.utils.nowdate(),
			"mode_of_payment": mode_of_payment,
			"party_type": "Customer",
			"party": customer,
			"party_account": party_account,
			"paid_from": party_account,
			# Given here, so ERPNext does not look the accounts up with the desk user's rights.
			"paid_from_account_currency": frm.account_currency,
			"paid_from_account_type": frm.account_type,
			"paid_to": cash_account,
			"paid_to_account_currency": to.account_currency,
			"paid_to_account_type": to.account_type,
			"paid_amount": amount,
			"received_amount": amount,
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"reference_no": request_id or shift.name,
			"reference_date": frappe.utils.nowdate(),
			"remarks": _("Debt collected in shift {0}").format(shift.name),
			"resto_shift": shift.name,
		}
	)
	left = amount
	for d in debts:
		if left <= 0.005:
			break
		part = min(left, flt(d.outstanding_amount))
		pe.append(
			"references",
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": d.name,
				"total_amount": flt(d.rounded_total or d.grand_total),
				"outstanding_amount": flt(d.outstanding_amount),
				"allocated_amount": flt(part, ACCOUNTS_PRECISION),
			},
		)
		left -= part
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name
