"""What each worker still owes of the salary advances paid from the drawer (D-237): what the
manager takes off each salary at the month's end (the salary entry credits the Staff Advances
Account for the worker), read from the General Ledger, so a deduction made shows at once."""

import frappe
from frappe import _
from frappe.utils import cint, nowdate

from resto.resto.staff_advance import open_advances
from resto.resto.utils import get_settings


def execute(filters=None):
	filters = frappe._dict(filters or {})
	settings = get_settings()
	company = filters.get("company") or settings.company
	account = settings.get("staff_advance_account")
	if not account:
		frappe.msgprint(_("Set the Staff Advances Account in Resto Settings first"))
		return columns(), []
	rows = open_advances(company, account, filters.get("to_date") or nowdate())
	if not cint(filters.get("show_settled")):
		rows = [r for r in rows if abs(r.owed) >= 0.005]
	return columns(), rows


def columns():
	return [
		{"fieldname": "employee", "label": _("Employee"), "fieldtype": "Link", "options": "Employee", "width": 130},
		{"fieldname": "employee_name", "label": _("Employee Name"), "fieldtype": "Data", "width": 180},
		{"fieldname": "advances", "label": _("Number of Advances"), "fieldtype": "Int", "width": 90},
		{"fieldname": "last_advance", "label": _("Last Advance"), "fieldtype": "Date", "width": 110},
		{"fieldname": "advanced", "label": _("Paid as Advances"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "deducted", "label": _("Paid Back or Taken Off Salaries"), "fieldtype": "Currency", "width": 160},
		{"fieldname": "owed", "label": _("Owed by the Worker"), "fieldtype": "Currency", "width": 120},
	]
