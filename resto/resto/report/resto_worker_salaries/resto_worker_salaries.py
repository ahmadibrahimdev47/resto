"""What each worker was paid, month by month (D-261): read from the submitted salary entries, with
the advance taken off and the net, and links to the salary entry and its Journal Entry."""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, format_date, get_first_day, nowdate

from resto.resto.utils import get_settings


def execute(filters=None):
	filters = frappe._dict(filters or {})
	filters.company = filters.company or get_settings().company
	filters.to_month = get_first_day(filters.to_month or nowdate())
	filters.from_month = get_first_day(filters.from_month or add_months(filters.to_month, -11))
	if filters.from_month > filters.to_month:
		frappe.throw(_("From Month must be before To Month"))
	cond = " and w.employee = %(employee)s" if filters.employee else ""
	rows = frappe.db.sql(
		f"""
		select e.salary_month, w.employee, w.employee_name, emp.designation, w.salary,
			w.deduct_amount as deducted, w.net, e.name as salary_entry, e.journal_entry
		from `tabResto Salary Entry Worker` w
		join `tabResto Salary Entry` e on e.name = w.parent and w.parenttype = 'Resto Salary Entry'
		left join `tabEmployee` emp on emp.name = w.employee
		where e.docstatus = 1 and e.company = %(company)s
			and e.salary_month between %(from_month)s and %(to_month)s{cond}
		order by e.salary_month, w.employee_name
		""",
		filters,
		as_dict=True,
	)
	for r in rows:
		r.month = format_date(r.salary_month, "MM-yyyy")
	if cint(filters.by_worker):
		return columns(by_worker=True), by_worker(rows)
	return columns(), rows


def by_worker(rows):
	out = {}
	for r in rows:
		d = out.setdefault(r.employee, frappe._dict(employee=r.employee, employee_name=r.employee_name, designation=r.designation, months=0, salary=0, deducted=0, net=0))
		d.months += 1
		d.salary = flt(d.salary + flt(r.salary), 2)
		d.deducted = flt(d.deducted + flt(r.deducted), 2)
		d.net = flt(d.net + flt(r.net), 2)
	return sorted(out.values(), key=lambda d: d.employee_name or d.employee)


def columns(by_worker=False):
	head = (
		[{"fieldname": "months", "label": _("Months"), "fieldtype": "Int", "width": 80}]
		if by_worker
		else [{"fieldname": "month", "label": _("Month"), "fieldtype": "Data", "width": 90}]
	)
	money = [
		{"fieldname": "salary", "label": _("Salary"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "deducted", "label": _("Advance Taken Off"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "net", "label": _("Net Paid"), "fieldtype": "Currency", "width": 120},
	]
	links = (
		[]
		if by_worker
		else [
			{"fieldname": "salary_entry", "label": _("Salary Entry"), "fieldtype": "Link", "options": "Resto Salary Entry", "width": 150},
			{"fieldname": "journal_entry", "label": _("Journal Entry"), "fieldtype": "Link", "options": "Journal Entry", "width": 160},
		]
	)
	return [
		*head,
		{"fieldname": "employee", "label": _("Employee"), "fieldtype": "Link", "options": "Employee", "width": 120},
		{"fieldname": "employee_name", "label": _("Employee Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "designation", "label": _("Designation"), "fieldtype": "Data", "width": 120},
		*money,
		*links,
	]
