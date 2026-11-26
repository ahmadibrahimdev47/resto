"""A salary advance paid to a worker out of the drawer (D-237).

The worker owes it until it is taken off their salary, so it is not an expense: a Journal Entry
debits the Staff Advances Account of Resto Settings (a receivable account, the worker as Employee
party) and credits the cash method's account. The shift counts it as money out of the drawer; the
month's salary entry credits the same account for the worker, which closes what they owe.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from resto.resto import approval
from resto.resto.utils import CASHIER, get_settings, has_role, require_role

ACCOUNTS_PRECISION = 2


def account_problem(account, company):
	"""Why `account` cannot hold staff advances, or None."""
	acc = frappe.db.get_value(
		"Account", account, ["company", "is_group", "disabled", "root_type", "account_type"], as_dict=True
	)
	if not acc or acc.company != company or cint(acc.is_group) or cint(acc.disabled):
		return _("The Staff Advances Account {0} must be an account of company {1}, not a group").format(
			frappe.bold(account), frappe.bold(company)
		)
	if acc.root_type != "Asset" or acc.account_type != "Receivable":
		return _(
			"The Staff Advances Account {0} must be an asset account of type Receivable: the worker owes the advance until it is taken off their salary"
		).format(frappe.bold(account))
	return None


def account(company):
	"""The Staff Advances Account of Resto Settings, checked."""
	name = get_settings().get("staff_advance_account")
	if not name:
		frappe.throw(_("Set the Staff Advances Account in Resto Settings first"))
	problem = account_problem(name, company)
	if problem:
		frappe.throw(problem)
	return name


def _employee(employee, company):
	emp = frappe.db.get_value("Employee", employee, ["employee_name", "company", "status"], as_dict=True)
	if not emp:
		frappe.throw(_("Choose the worker"))
	if emp.company != company:
		frappe.throw(_("{0} is not a worker of {1}").format(emp.employee_name or employee, company))
	if emp.status != "Active":
		frappe.throw(_("{0} is not an active worker").format(emp.employee_name or employee))
	return emp.employee_name or employee


@frappe.whitelist()
def advance_data():
	"""What the Staff Advance dialog needs: the shift's cash methods and the approval limit."""
	from resto.resto import shift as shifts

	require_role(CASHIER)
	shift = shifts._own_shift()
	account(shift.company)
	cap = flt(get_settings().get("staff_advance_cap"))
	return {
		"company": shift.company,
		"modes": shifts._cash_modes(shift),
		# Read here, not by the screen: the cashier only picks a worker (select on Employee).
		"employees": frappe.get_all(
			"Employee",
			filters={"company": shift.company, "status": "Active"},
			fields=["name", "employee_name"],
			order_by="employee_name asc",
		),
		# Above this a manager's PIN is asked (None: never, or the user is a manager).
		"approval_above": cap if cap and not has_role() else None,
	}


@frappe.whitelist(methods=["POST"])
def add_advance(employee, amount, mode_of_payment, remarks=None, request_id=None, approver=None, pin=None):
	"""Pay a worker an advance out of the drawer: a Journal Entry into the Staff Advances Account
	with the worker as party, counted by the shift (its expected amount goes down)."""
	from resto.resto import shift as shifts

	require_role(CASHIER)
	shift = shifts._own_shift(lock=True)
	if request_id:
		# The same click again, after a lost answer: the entry already made.
		done = next((a for a in shift.get("advances") or [] if a.request_id == request_id), None)
		if done:
			return done.journal_entry
	amount = flt(amount, ACCOUNTS_PRECISION)
	if amount <= 0:
		frappe.throw(_("The advance must be more than zero"))
	if mode_of_payment not in shifts._cash_modes(shift):
		frappe.throw(_("Payment method {0} is not a cash method of this shift").format(frappe.bold(mode_of_payment)))
	advance_account = account(shift.company)
	name = _employee(employee, shift.company)
	row = next((r for r in shifts.compute(shift)["rows"] if r["mode_of_payment"] == mode_of_payment), None)
	if not row or amount > row["expected_amount"] + 0.005:
		if shifts._blind() or not row:
			frappe.throw(_("The drawer does not hold that much for {0}").format(mode_of_payment))
		frappe.throw(
			_("Only {0} should be in the drawer for {1}").format(frappe.format(row["expected_amount"], "Currency"), mode_of_payment)
		)
	approved_by = None
	cap = flt(get_settings().get("staff_advance_cap"))
	if cap and amount > cap + 0.005:
		approved_by = approval.approve("staff_advance", shift.name, amount, approver, pin, details=name)
	note = (remarks or "").strip()
	je = frappe.new_doc("Journal Entry")
	je.update({"voucher_type": "Cash Entry", "company": shift.company, "posting_date": nowdate()})
	shifts._set_remark(
		je,
		_("Staff advance, shift {0}: {1}").format(shift.name, name) + (f" ({note})" if note else ""),
	)
	je.append(
		"accounts",
		{"account": advance_account, "party_type": "Employee", "party": employee, "debit_in_account_currency": amount},
	)
	je.append(
		"accounts",
		{
			"account": shifts._mode_account(mode_of_payment, shift.company),
			"credit_in_account_currency": amount,
			"cost_center": shifts._cost_center(shift),
		},
	)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	shift.append(
		"advances",
		{
			"mode_of_payment": mode_of_payment,
			"employee": employee,
			"employee_name": name,
			"amount": amount,
			"remarks": note,
			"journal_entry": je.name,
			"approved_by": approved_by,
			"currency": shift.currency,
			"request_id": request_id,
		},
	)
	shift.flags.resto_shift = True
	shift.save(ignore_permissions=True)
	return je.name


def live(shift):
	"""The shift's advances whose Journal Entry is still in force."""
	rows = [a for a in shift.get("advances") or [] if a.journal_entry]
	if not rows:
		return []
	alive = set(
		frappe.get_all("Journal Entry", filters={"name": ("in", [a.journal_entry for a in rows]), "docstatus": 1}, pluck="name")
	)
	return [a for a in rows if a.journal_entry in alive]


def open_advances(company, account_name, to_date=None):
	"""What each worker owes on the Staff Advances Account up to `to_date`: advanced (debits),
	taken off (credits) and still owed, from the General Ledger."""
	rows = frappe.db.sql(
		"""select party as employee, sum(debit) as advanced, sum(credit) as deducted,
			sum(debit) - sum(credit) as owed, max(case when debit > 0 then posting_date end) as last_advance,
			sum(case when debit > 0 then 1 else 0 end) as advances
		from `tabGL Entry`
		where company = %(company)s and account = %(account)s and party_type = 'Employee'
			and is_cancelled = 0 and posting_date <= %(to_date)s
		group by party
		order by owed desc""",
		{"company": company, "account": account_name, "to_date": getdate(to_date or nowdate())},
		as_dict=True,
	)
	for r in rows:
		r.employee_name = frappe.db.get_value("Employee", r.employee, "employee_name") or r.employee
		for k in ("advanced", "deducted", "owed"):
			r[k] = flt(r[k], ACCOUNTS_PRECISION)
	return rows
