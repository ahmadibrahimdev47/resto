"""The month's salary entry (D-249): one Journal Entry for all the workers' monthly salaries.

Salaries are paid by a monthly Journal Entry, not by HRMS payroll (salary structures, slips and
payroll entries are more than a restaurant's fixed monthly pay needs). This document makes that
entry exact and hard to get wrong:
- each worker's fixed monthly salary comes from their Employee record (Monthly Salary, permlevel 1);
- an open staff advance (D-237) is taken off when ticked, never more than the worker still owes
  nor than the salary; unticked, it stays owed for a later month;
- Dr the Salaries Account of Resto Settings for the salaries, Cr the Staff Advances Account for
  each advance taken off (the worker as party), Cr the account paid from for the net;
- a month is posted once per company: another one is refused until the first is cancelled, and
  cancelling it cancels its Journal Entry.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, format_date, get_first_day, nowdate

from resto.resto.utils import get_settings

ACCOUNTS_PRECISION = 2
MANAGERS = ("System Manager", "Resto Manager", "Accounts Manager")
PAY_FROM_TYPES = ("Cash", "Bank")


def salary_account_problem(account, company):
	"""Why `account` cannot be the Salaries Account, or None."""
	acc = frappe.db.get_value("Account", account, ["company", "is_group", "disabled", "root_type"], as_dict=True)
	if not acc or acc.company != company or cint(acc.is_group) or cint(acc.disabled):
		return _("The Salaries Account {0} must be an account of company {1}, not a group").format(frappe.bold(account), frappe.bold(company))
	if acc.root_type != "Expense":
		return _("The Salaries Account {0} must be an expense account").format(frappe.bold(account))
	return None


def pay_from_problem(account, company):
	acc = frappe.db.get_value("Account", account, ["company", "is_group", "disabled", "account_type"], as_dict=True)
	if not acc or acc.company != company or cint(acc.is_group) or cint(acc.disabled):
		return _("{0} must be an account of company {1}, not a group").format(frappe.bold(account), frappe.bold(company))
	if acc.account_type not in PAY_FROM_TYPES:
		return _("Salaries are paid from a cash or bank account, not {0}").format(frappe.bold(account))
	return None


def _advances_account(company):
	"""The Staff Advances Account of Resto Settings when it is fit, else None (no advance to take off)."""
	from resto.resto import staff_advance

	name = get_settings().get("staff_advance_account")
	if name and not staff_advance.account_problem(name, company):
		return name
	return None


def _owed(company, account):
	"""What each worker still owes on the Staff Advances Account, by Employee."""
	from resto.resto import staff_advance

	if not account:
		return {}
	return {r.employee: flt(r.owed, ACCOUNTS_PRECISION) for r in staff_advance.open_advances(company, account)}


def _require_manager():
	if not set(frappe.get_roles()) & set(MANAGERS):
		frappe.throw(_("You are not allowed to do this."), frappe.PermissionError)


@frappe.whitelist()
def new_entry_defaults():
	"""What a new salary entry starts with: the company, the account paid from (the main safe) and
	the accounts of Resto Settings."""
	_require_manager()
	settings = get_settings()
	company = settings.get("company")
	return {
		"company": company,
		"pay_from_account": settings.get("main_safe_account"),
		"salary_account": settings.get("salary_account"),
		"advances_account": _advances_account(company) if company else None,
	}


@frappe.whitelist()
def get_workers(company):
	"""The company's active workers with their monthly salary and open advance: each advance is
	taken off in full when the salary covers it, else up to the salary."""
	_require_manager()
	owed = _owed(company, _advances_account(company))
	rows = []
	for e in frappe.get_all(
		"Employee",
		filters={"company": company, "status": "Active"},
		fields=["name", "employee_name", "resto_monthly_salary"],
		order_by="employee_name asc",
	):
		salary = flt(e.resto_monthly_salary, ACCOUNTS_PRECISION)
		open_advance = max(owed.get(e.name, 0), 0)
		if not salary and not open_advance:
			continue
		deduct_amount = min(open_advance, salary)
		rows.append(
			{
				"employee": e.name,
				"employee_name": e.employee_name,
				"salary": salary,
				"owed": open_advance,
				"deduct": 1 if deduct_amount > 0 else 0,
				"deduct_amount": deduct_amount,
				"net": flt(salary - deduct_amount, ACCOUNTS_PRECISION),
			}
		)
	# Workers who left but still owe are taken off by hand: the list holds the active ones.
	return rows


class RestoSalaryEntry(Document):
	def validate(self):
		settings = get_settings()
		self.company = self.company or settings.get("company")
		self.salary_month = get_first_day(self.salary_month or nowdate())
		if self.docstatus == 0:
			# The accounts of Resto Settings at the time it is posted, kept with it.
			self.salary_account = settings.get("salary_account")
			self.advances_account = _advances_account(self.company)
			self.pay_from_account = self.pay_from_account or settings.get("main_safe_account")
		if not self.salary_account:
			frappe.throw(_("Set the Salaries Account in Resto Settings first"))
		problem = salary_account_problem(self.salary_account, self.company)
		if problem:
			frappe.throw(problem)
		if not self.pay_from_account:
			frappe.throw(_("Choose the account the salaries are paid from"))
		problem = pay_from_problem(self.pay_from_account, self.company)
		if problem:
			frappe.throw(problem)
		self._check_workers()
		if self.docstatus == 0:
			other = self._other_entry()
			if other:
				frappe.msgprint(
					_("The salaries of {0} are already posted in {1}: this one cannot be posted unless that one is cancelled").format(
						self.month_label(), frappe.bold(other)
					),
					indicator="orange",
					alert=True,
				)

	def month_label(self):
		return format_date(self.salary_month, "MM-yyyy")

	def _check_workers(self):
		if not self.workers:
			frappe.throw(_("Add the workers: Get Workers fills them in"))
		owed = _owed(self.company, self.advances_account)
		seen = set()
		total = deducted = 0
		for row in self.workers:
			emp = frappe.db.get_value("Employee", row.employee, ["employee_name", "company"], as_dict=True)
			if not emp or emp.company != self.company:
				frappe.throw(_("Row {0}: {1} is not a worker of {2}").format(row.idx, row.employee, self.company))
			if row.employee in seen:
				frappe.throw(_("Row {0}: {1} is listed twice").format(row.idx, emp.employee_name or row.employee))
			seen.add(row.employee)
			row.employee_name = emp.employee_name or row.employee
			row.salary = flt(row.salary, ACCOUNTS_PRECISION)
			if row.salary < 0:
				frappe.throw(_("Row {0}: the salary cannot be negative").format(row.idx))
			row.owed = max(owed.get(row.employee, 0), 0)
			row.deduct_amount = flt(row.deduct_amount, ACCOUNTS_PRECISION) if cint(row.deduct) else 0
			if row.deduct_amount < 0:
				frappe.throw(_("Row {0}: what is taken off cannot be negative").format(row.idx))
			if row.deduct_amount > row.owed + 0.005:
				frappe.throw(
					_("Row {0}: {1} owes {2} only").format(row.idx, row.employee_name, frappe.format(row.owed, "Currency"))
				)
			if row.deduct_amount > row.salary + 0.005:
				frappe.throw(_("Row {0}: what is taken off is more than the salary of {1}").format(row.idx, row.employee_name))
			if row.deduct_amount and not self.advances_account:
				frappe.throw(_("Set the Staff Advances Account in Resto Settings first"))
			row.net = flt(row.salary - row.deduct_amount, ACCOUNTS_PRECISION)
			total += row.salary
			deducted += row.deduct_amount
		self.total_salaries = flt(total, ACCOUNTS_PRECISION)
		self.total_deducted = flt(deducted, ACCOUNTS_PRECISION)
		self.total_net = flt(total - deducted, ACCOUNTS_PRECISION)
		if self.total_salaries <= 0:
			frappe.throw(_("There are no salaries to post"))

	def _other_entry(self):
		return frappe.db.get_value(
			"Resto Salary Entry",
			{"company": self.company, "salary_month": self.salary_month, "docstatus": 1, "name": ("!=", self.name)},
		)

	def before_submit(self):
		# One salary entry of a company at a time, so a month is never posted twice, and each
		# worker locked, so what they owe is read after any repayment committed now.
		frappe.db.sql("select name from `tabCompany` where name = %s for update", self.company)
		other = self._other_entry()
		if other:
			frappe.throw(
				_("The salaries of {0} are already posted in {1}: cancel it first to post them again").format(
					self.month_label(), frappe.bold(other)
				)
			)
		for row in self.workers:
			frappe.db.sql("select name from `tabEmployee` where name = %s for update", row.employee)
		self._check_workers()

	def on_submit(self):
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")
		je = frappe.new_doc("Journal Entry")
		je.update({"voucher_type": "Journal Entry", "company": self.company, "posting_date": self.posting_date})
		from resto.resto import shift as shifts

		text = _("Salaries of {0}, {1} workers ({2})").format(self.month_label(), len(self.workers), self.name)
		shifts._set_remark(je, text + (f": {self.remarks}" if self.remarks else ""))
		je.append("accounts", {"account": self.salary_account, "debit_in_account_currency": self.total_salaries, "cost_center": cost_center})
		for row in self.workers:
			if row.deduct_amount:
				je.append(
					"accounts",
					{
						"account": self.advances_account,
						"party_type": "Employee",
						"party": row.employee,
						"credit_in_account_currency": row.deduct_amount,
						"user_remark": _("Advance taken off the salary of {0}").format(self.month_label()),
					},
				)
		if self.total_net:
			je.append("accounts", {"account": self.pay_from_account, "credit_in_account_currency": self.total_net, "cost_center": cost_center})
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		self.db_set("journal_entry", je.name)

	def on_cancel(self):
		if self.journal_entry and frappe.db.get_value("Journal Entry", self.journal_entry, "docstatus") == 1:
			je = frappe.get_doc("Journal Entry", self.journal_entry)
			je.flags.resto_salary_cancel = True
			je.flags.ignore_permissions = True
			je.cancel()


def check_journal_cancel(doc, method=None):
	"""before_cancel of Journal Entry: a salary entry's Journal Entry is cancelled with it."""
	if doc.flags.resto_salary_cancel:
		return
	name = frappe.db.get_value("Resto Salary Entry", {"journal_entry": doc.name, "docstatus": 1})
	if name:
		frappe.throw(_("{0} is the entry of salary entry {1}: cancel the salary entry instead").format(doc.name, name))
