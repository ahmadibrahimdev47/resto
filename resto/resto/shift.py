"""The cashier's shift, kept by Resto (D-203): opened and closed from the floor screen.

A shift belongs to one cashier on the restaurant's POS Profile, and it may run past midnight. A
bill settled on the floor is linked to the cashier's open shift (Sales Invoice.resto_shift); the
closing counts what those invoices received, per payment method, from their own payment rows.
Drawer expenses are Journal Entries from the payment method's account. Only the counted amounts
come from the browser: everything expected is worked out again on the server when the shift closes.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from resto.resto.utils import CASHIER, get_settings, require_role

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
	"""The user's open shift (on that POS Profile, when given), or None. lock: a locking read, so a
	close committed by another request is seen and a close running now is waited for."""
	filters = {"user": user, "status": "Open"}
	if pos_profile:
		filters["pos_profile"] = pos_profile
	name = frappe.db.get_value("Resto Shift", filters, "name", order_by="opening_time desc")
	if name and lock:
		if frappe.db.get_value("Resto Shift", name, "status", for_update=True) != "Open":
			return None
	return name


def _lock_user():
	"""One shift action of a user at a time (a double tap, two devices)."""
	frappe.db.sql("select name from `tabUser` where name = %s for update", frappe.session.user)


def _own_shift(lock=False):
	if lock:
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
			if row:
				change[row.mode_of_payment] += flt(i.change_amount)

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

	rows = []
	for m in modes:
		expected = flt(opening[m] + received[m] - change[m] - spent.get(m, 0), ACCOUNTS_PRECISION)
		rows.append(
			{
				"mode_of_payment": m,
				"opening_amount": flt(opening[m], ACCOUNTS_PRECISION),
				"received_amount": flt(received[m], ACCOUNTS_PRECISION),
				"change_amount": flt(change[m], ACCOUNTS_PRECISION),
				"expenses_amount": flt(spent.get(m, 0), ACCOUNTS_PRECISION),
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
	collected = sum(r["paid_amount"] for r in inv_rows)
	expenses_total = sum(flt(e.amount) for e in expenses)
	return {
		"rows": rows,
		"invoices": inv_rows,
		"taxes": [{"account_head": t.account_head, "rate": flt(t.rate), "amount": flt(t.amount, ACCOUNTS_PRECISION)} for t in taxes],
		"expenses": [
			{"mode_of_payment": e.mode_of_payment, "expense_account": e.expense_account, "amount": flt(e.amount), "remarks": e.remarks, "journal_entry": e.journal_entry}
			for e in expenses
		],
		"summary": {
			"sales_count": len(sales),
			"gross_sales": flt(gross, ACCOUNTS_PRECISION),
			"returns_count": len(returns),
			"returns_total": flt(returns_total, ACCOUNTS_PRECISION),
			"net_sales": flt(gross - returns_total, ACCOUNTS_PRECISION),
			"collected": flt(collected, ACCOUNTS_PRECISION),
			"outstanding": flt(sum(r["on_account"] for r in inv_rows), ACCOUNTS_PRECISION),
			"expenses_count": len(expenses),
			"expenses_total": flt(expenses_total, ACCOUNTS_PRECISION),
			"net_cash": flt(collected - expenses_total, ACCOUNTS_PRECISION),
			"tax_total": flt(sum(flt(t.amount) for t in taxes), ACCOUNTS_PRECISION),
			"total_quantity": flt(sum(flt(i.total_qty) for i in invoices)),
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
			"net_cash": s["net_cash"],
			"total_difference": flt(sum(r["difference"] for r in figures["rows"]), ACCOUNTS_PRECISION),
		}
	)
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	report = _report(shift, figures)
	report["closing"] = shift.name
	return report


# --------------------------------------------------------------------------- drawer expenses


@frappe.whitelist()
def expense_accounts():
	"""The expense accounts a drawer expense can go to, and the shift's payment methods."""
	require_role(CASHIER)
	shift = _own_shift()
	accounts = frappe.get_all(
		"Account",
		filters={"company": shift.company, "root_type": "Expense", "is_group": 0, "disabled": 0},
		fields=["name", "account_name"],
		order_by="lft",
	)
	return {"accounts": accounts, "modes": [p.mode_of_payment for p in shift.payments]}


@frappe.whitelist(methods=["POST"])
def add_expense(mode_of_payment, expense_account, amount, remarks=None):
	"""Money paid out of the drawer: a Journal Entry from the payment method's account to the
	expense account, counted by the shift (its expected amount goes down)."""
	require_role(CASHIER)
	shift = _own_shift(lock=True)
	amount = flt(amount, ACCOUNTS_PRECISION)
	if amount <= 0:
		frappe.throw(_("The expense amount must be more than zero"))
	if mode_of_payment not in [p.mode_of_payment for p in shift.payments]:
		frappe.throw(_("Payment method {0} is not in this shift").format(frappe.bold(mode_of_payment)))
	acc = frappe.db.get_value("Account", expense_account, ["company", "root_type", "is_group", "disabled"], as_dict=True)
	if not acc or acc.company != shift.company or acc.root_type != "Expense" or cint(acc.is_group) or cint(acc.disabled):
		frappe.throw(_("{0} is not an expense account of {1}").format(expense_account, shift.company))
	if not (remarks or "").strip():
		frappe.throw(_("Write what the expense was for"))
	row = next(r for r in compute(shift)["rows"] if r["mode_of_payment"] == mode_of_payment)
	if amount > row["expected_amount"] + 0.005:
		frappe.throw(
			_("Only {0} should be in the drawer for {1}").format(frappe.format(row["expected_amount"], "Currency"), mode_of_payment)
		)
	cost_center = frappe.db.get_value("POS Profile", shift.pos_profile, "cost_center") or frappe.get_cached_value(
		"Company", shift.company, "cost_center"
	)
	je = frappe.new_doc("Journal Entry")
	je.update(
		{
			"voucher_type": "Cash Entry",
			"company": shift.company,
			"posting_date": frappe.utils.nowdate(),
			"user_remark": _("Drawer expense, shift {0}: {1}").format(shift.name, remarks.strip()),
		}
	)
	je.append("accounts", {"account": expense_account, "debit_in_account_currency": amount, "cost_center": cost_center})
	je.append(
		"accounts",
		{"account": _mode_account(mode_of_payment, shift.company), "credit_in_account_currency": amount, "cost_center": cost_center},
	)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	shift.append(
		"expenses",
		{
			"mode_of_payment": mode_of_payment,
			"expense_account": expense_account,
			"amount": amount,
			"remarks": remarks.strip(),
			"journal_entry": je.name,
			"currency": shift.currency,
		},
	)
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	return je.name
