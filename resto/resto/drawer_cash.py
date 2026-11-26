"""Cash put into the drawer and cash taken out to the main safe, during a shift (D-245).

Like a drawer expense or a staff advance, each is a Journal Entry on the drawer's cash account that
the shift counts on its own line, so the close explains it instead of showing it as over or short.
The cashier never picks a free account:
- Cash In, a staff advance paid back: from the worker's open advance (Staff Advances Account, the
  worker as Employee party), up to what they still owe;
- Cash In, change from the main safe: from the Main Safe Account of Resto Settings, with a manager's PIN;
- Cash In, another reason: from one of the Drawer Cash In Accounts of Resto Settings, with a note and
  a manager's PIN;
- Cash Drop to the main safe: into the Main Safe Account, with a manager's PIN, never more than the
  drawer should hold. Right after the close it takes from what was counted instead (the money left
  for the safe at the end of the day); the closed shift keeps it apart from what it counted.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime, nowdate

from resto.resto import approval
from resto.resto.utils import CASHIER, get_settings, has_role, require_role

ACCOUNTS_PRECISION = 2
CASH_IN, CASH_DROP = "Cash In", "Cash Drop"
ADVANCE, CHANGE, OTHER, TO_SAFE = "Staff Advance Repaid", "Change from the Main Safe", "Other", "To the Main Safe"
# A drop after the close is taken from the cashier's last shift, closed this long ago at most.
AFTER_CLOSE_HOURS = 12
# Accounts money cannot come in from on the "other" line: a party is needed, the worker's and the
# safe's lines exist for them.
NOT_OTHER_TYPES = ("Receivable", "Payable")


# --------------------------------------------------------------------------- accounts


def main_safe_problem(account, company):
	"""Why `account` cannot be the main safe, or None."""
	acc = frappe.db.get_value(
		"Account", account, ["company", "is_group", "disabled", "root_type", "account_type"], as_dict=True
	)
	if not acc or acc.company != company or cint(acc.is_group) or cint(acc.disabled):
		return _("The Main Safe Account {0} must be an account of company {1}, not a group").format(
			frappe.bold(account), frappe.bold(company)
		)
	if acc.root_type != "Asset" or acc.account_type != "Cash":
		return _("The Main Safe Account {0} must be an asset account of type Cash").format(frappe.bold(account))
	return None


def main_safe(company, drawer_account):
	"""The Main Safe Account of Resto Settings, checked, never the drawer's own account."""
	name = get_settings().get("main_safe_account")
	if not name:
		frappe.throw(_("Set the Main Safe Account in Resto Settings first"))
	problem = main_safe_problem(name, company)
	if problem:
		frappe.throw(problem)
	if name == drawer_account:
		frappe.throw(_("The Main Safe Account {0} is the drawer's own account: choose the safe's account in Resto Settings").format(frappe.bold(name)))
	return name


def other_problem(account, company):
	"""Why `account` cannot be a Drawer Cash In Account, or None."""
	acc = frappe.db.get_value("Account", account, ["company", "is_group", "disabled", "account_type"], as_dict=True)
	if not acc or acc.company != company or cint(acc.is_group) or cint(acc.disabled):
		return _("{0} must be an account of company {1}, not a group").format(frappe.bold(account), frappe.bold(company))
	if acc.account_type in NOT_OTHER_TYPES:
		return _("{0} is a receivable or payable account: money from a customer, a supplier or a worker has its own way in").format(
			frappe.bold(account)
		)
	return None


def other_accounts(company):
	"""The Drawer Cash In Accounts of Resto Settings that are still fit."""
	names = [r.account for r in get_settings().get("drawer_cash_in_accounts") or [] if r.account]
	return [
		{"name": n, "account_name": frappe.db.get_value("Account", n, "account_name") or n}
		for n in names
		if not other_problem(n, company)
	]


def _owed(company, employee):
	"""What the worker still owes on the Staff Advances Account, or 0 without the account."""
	from resto.resto import staff_advance

	name = get_settings().get("staff_advance_account")
	if not name or staff_advance.account_problem(name, company):
		return 0
	row = next((r for r in staff_advance.open_advances(company, name) if r.employee == employee), None)
	return flt(row.owed if row else 0, ACCOUNTS_PRECISION)


# --------------------------------------------------------------------------- the shift's moves


def live(shift, after_close=False):
	"""The shift's moves whose Journal Entry is in force: during the shift (counted by the close), or
	after it (after_close)."""
	rows = [m for m in shift.get("cash_moves") or [] if m.journal_entry and cint(m.after_close) == cint(after_close)]
	if not rows:
		return []
	alive = set(
		frappe.get_all("Journal Entry", filters={"name": ("in", [m.journal_entry for m in rows]), "docstatus": 1}, pluck="name")
	)
	return [m for m in rows if m.journal_entry in alive]


def _done(shift, request_id):
	if not request_id:
		return None
	row = next((m for m in shift.get("cash_moves") or [] if m.request_id == request_id), None)
	return row.journal_entry if row else None


def _post(shift, debit, credit, amount, text, credit_party=None):
	"""Debit `debit`, credit `credit` (with the worker as party when given), posted."""
	from resto.resto import shift as shifts

	cost_center = shifts._cost_center(shift)
	je = frappe.new_doc("Journal Entry")
	je.update({"voucher_type": "Cash Entry", "company": shift.company, "posting_date": nowdate()})
	shifts._set_remark(je, text)
	je.append("accounts", {"account": debit, "debit_in_account_currency": amount, "cost_center": cost_center})
	row = {"account": credit, "credit_in_account_currency": amount, "cost_center": cost_center}
	if credit_party:
		row.update({"party_type": "Employee", "party": credit_party})
	je.append("accounts", row)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def _worker(employee, company):
	"""The worker's name; one who left may still pay back what they owe."""
	emp = frappe.db.get_value("Employee", employee, ["employee_name", "company"], as_dict=True) if employee else None
	if not emp:
		frappe.throw(_("Choose the worker"))
	if emp.company != company:
		frappe.throw(_("{0} is not a worker of {1}").format(emp.employee_name or employee, company))
	return emp.employee_name or employee


def _record(shift, je, **row):
	shift.append("cash_moves", {"journal_entry": je, "currency": shift.currency, **row})
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	return je


def _amount(amount):
	amount = flt(amount, ACCOUNTS_PRECISION)
	if amount <= 0:
		frappe.throw(_("The amount must be more than zero"))
	return amount


# --------------------------------------------------------------------------- cash in


@frappe.whitelist()
def cash_in_data():
	"""What the Cash In dialog offers: the shift's cash methods, the workers who owe an advance, the
	other accounts, and whether the main safe is set."""
	from resto.resto import shift as shifts

	require_role(CASHIER)
	shift = shifts._own_shift()
	company = shift.company
	workers = []
	from resto.resto import staff_advance

	name = get_settings().get("staff_advance_account")
	if name and not staff_advance.account_problem(name, company):
		workers = [
			{"employee": r.employee, "employee_name": r.employee_name, "owed": flt(r.owed, ACCOUNTS_PRECISION)}
			for r in staff_advance.open_advances(company, name)
			if flt(r.owed) > 0.005
		]
	safe = get_settings().get("main_safe_account")
	return {
		"modes": shifts._cash_modes(shift),
		"workers": workers,
		"safe": bool(safe and not main_safe_problem(safe, company)),
		"accounts": other_accounts(company),
		# Change and other reasons always need a manager's PIN (none for a manager).
		"needs_pin": not has_role(),
	}


@frappe.whitelist(methods=["POST"])
def add_cash_in(reason, amount, mode_of_payment, employee=None, account=None, remarks=None, request_id=None, approver=None, pin=None):
	"""Money put into the drawer: a Journal Entry into the drawer's cash account, counted by the shift
	(its expected amount goes up)."""
	from resto.resto import shift as shifts
	from resto.resto import staff_advance

	require_role(CASHIER)
	shift = shifts._own_shift(lock=True)
	done = _done(shift, request_id)
	if done:
		return done  # the same click again, after a lost answer
	amount = _amount(amount)
	if mode_of_payment not in shifts._cash_modes(shift):
		frappe.throw(_("Payment method {0} is not a cash method of this shift").format(frappe.bold(mode_of_payment)))
	drawer = shifts._mode_account(mode_of_payment, shift.company)
	note = (remarks or "").strip()
	row = {"move": CASH_IN, "reason": reason, "mode_of_payment": mode_of_payment, "amount": amount, "remarks": note, "request_id": request_id}

	if reason == ADVANCE:
		advance_account = staff_advance.account(shift.company)
		name = _worker(employee, shift.company)
		# The worker, locked: two repayments of the same advance never both go through.
		frappe.db.sql("select name from `tabEmployee` where name = %s for update", employee)
		owed = _owed(shift.company, employee)
		if amount > owed + 0.005:
			frappe.throw(_("{0} owes {1} only").format(name, frappe.format(owed, "Currency")))
		text = _("Staff advance paid back, shift {0}: {1}").format(shift.name, name)
		je = _post(shift, drawer, advance_account, amount, text + (f" ({note})" if note else ""), credit_party=employee)
		return _record(shift, je, account=advance_account, employee=employee, employee_name=name, **row)

	if reason == CHANGE:
		safe = main_safe(shift.company, drawer)
		approved_by = approval.approve("drawer_cash_in", shift.name, amount, approver, pin, details=_(CHANGE))
		text = _("Change from the main safe, shift {0}").format(shift.name)
		je = _post(shift, drawer, safe, amount, text + (f" ({note})" if note else ""))
		return _record(shift, je, account=safe, approved_by=approved_by, **row)

	if reason == OTHER:
		if account not in {a["name"] for a in other_accounts(shift.company)}:
			frappe.throw(_("{0} is not one of the Drawer Cash In Accounts (Resto Settings)").format(frappe.bold(account or "")))
		if account == drawer:
			frappe.throw(_("{0} is the drawer's own account: money cannot come into the drawer from itself").format(frappe.bold(account)))
		if not note:
			frappe.throw(_("Write where the money came from"))
		approved_by = approval.approve("drawer_cash_in", shift.name, amount, approver, pin, details=note)
		je = _post(shift, drawer, account, amount, _("Cash into the drawer, shift {0}: {1}").format(shift.name, note))
		return _record(shift, je, account=account, approved_by=approved_by, **row)

	frappe.throw(_("Choose why the money is put into the drawer"))


# --------------------------------------------------------------------------- cash drop


def _last_closed(lock=False):
	"""The cashier's last shift, when it closed less than AFTER_CLOSE_HOURS ago."""
	suffix = " for update" if lock else ""
	row = frappe.db.sql(
		f"""select name, closing_time from `tabResto Shift` where user = %s and status = 'Closed'
		order by closing_time desc limit 1{suffix}""",
		frappe.session.user,
		as_dict=True,
	)
	if not row or not row[0].closing_time:
		return None
	if get_datetime(row[0].closing_time) < add_to_date(now_datetime(), hours=-AFTER_CLOSE_HOURS):
		return None
	return frappe.get_doc("Resto Shift", row[0].name)


def _left_after_close(shift, mode_of_payment):
	"""What was counted for the method at the close, less what was dropped since."""
	counted = next((flt(p.closing_amount) for p in shift.payments if p.mode_of_payment == mode_of_payment), 0)
	dropped = sum(flt(m.amount) for m in live(shift, after_close=True) if m.mode_of_payment == mode_of_payment)
	return flt(counted - dropped, ACCOUNTS_PRECISION)


def _drop_shift(lock=False):
	"""(shift, after_close): the cashier's open shift, or else their shift closed just now."""
	from resto.resto import shift as shifts

	if lock:
		shifts._fresh()
		shifts._lock_user()
	name = shifts.open_shift_of(frappe.session.user, lock=lock)
	if name:
		return frappe.get_doc("Resto Shift", name), False
	closed = _last_closed(lock=lock)
	if not closed:
		frappe.throw(_("You have no open shift, and no shift closed in the last {0} hours").format(AFTER_CLOSE_HOURS))
	return closed, True


@frappe.whitelist()
def drop_data():
	"""What the Cash Drop dialog shows: the cash methods and, after the close, what is left of the count."""
	from resto.resto import shift as shifts

	require_role(CASHIER)
	shift, after_close = _drop_shift()
	modes = shifts._cash_modes(shift)
	out = {
		"shift": shift.name,
		"after_close": after_close,
		"modes": modes,
		"safe": get_settings().get("main_safe_account"),
		"needs_pin": not has_role(),
	}
	if after_close:
		# The count is the cashier's own: shown also when counting blind.
		out["left"] = {m: _left_after_close(shift, m) for m in modes}
	return out


@frappe.whitelist(methods=["POST"])
def cash_drop(amount, mode_of_payment, remarks=None, request_id=None, approver=None, pin=None):
	"""Cash taken out of the drawer to the main safe, with a manager's PIN: a Journal Entry from the
	drawer's cash account into the Main Safe Account. During the shift its expected amount goes down;
	right after the close it is taken from what was counted."""
	from resto.resto import shift as shifts

	require_role(CASHIER)
	shift, after_close = _drop_shift(lock=True)
	done = _done(shift, request_id)
	if done:
		return done
	amount = _amount(amount)
	if mode_of_payment not in shifts._cash_modes(shift):
		frappe.throw(_("Payment method {0} is not a cash method of this shift").format(frappe.bold(mode_of_payment)))
	drawer = shifts._mode_account(mode_of_payment, shift.company)
	safe = main_safe(shift.company, drawer)
	if after_close:
		left = _left_after_close(shift, mode_of_payment)
		if amount > left + 0.005:
			frappe.throw(_("Only {0} is left of what was counted for {1}").format(frappe.format(max(left, 0), "Currency"), mode_of_payment))
	else:
		row = next((r for r in shifts.compute(shift)["rows"] if r["mode_of_payment"] == mode_of_payment), None)
		if not row or amount > row["expected_amount"] + 0.005:
			if shifts._blind() or not row:
				frappe.throw(_("The drawer does not hold that much for {0}").format(mode_of_payment))
			frappe.throw(
				_("Only {0} should be in the drawer for {1}").format(frappe.format(row["expected_amount"], "Currency"), mode_of_payment)
			)
	approved_by = approval.approve("cash_drop", shift.name, amount, approver, pin, details=mode_of_payment)
	note = (remarks or "").strip()
	text = (
		_("Cash drop to the main safe after the close, shift {0}") if after_close else _("Cash drop to the main safe, shift {0}")
	).format(shift.name)
	je = _post(shift, safe, drawer, amount, text + (f" ({note})" if note else ""))
	return _record(
		shift,
		je,
		move=CASH_DROP,
		reason=TO_SAFE,
		after_close=1 if after_close else 0,
		mode_of_payment=mode_of_payment,
		account=safe,
		amount=amount,
		remarks=note,
		approved_by=approved_by,
		request_id=request_id,
	)
