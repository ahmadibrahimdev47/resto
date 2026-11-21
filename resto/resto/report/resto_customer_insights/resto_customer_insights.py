"""Customers: visits, spend, new or returning, and the last visit."""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from resto.resto.report_utils import invoice_condition, prepare_filters


def execute(filters=None):
	filters = prepare_filters(filters)
	walk_in = set(frappe.get_all("POS Profile", pluck="customer", filters={"customer": ("is", "set")}))
	cond = ""
	if not cint(filters.get("include_walk_in")) and walk_in:
		filters.walk_in = list(walk_in)
		cond = "and si.customer not in %(walk_in)s"
	data = frappe.db.sql(
		f"""
		select si.customer, max(si.customer_name) as customer_name,
			sum(case when si.is_return = 0 then 1 else 0 end) as visits,
			sum(si.base_net_total) as net_sales,
			max(si.posting_date) as last_visit,
			(select min(x.posting_date) from `tabSales Invoice` x where x.customer = si.customer and x.docstatus = 1) as first_visit,
			(select c.mobile_no from `tabCustomer` c where c.name = si.customer) as mobile
		from `tabSales Invoice` si
		where {invoice_condition()} {cond}
		group by si.customer
		order by net_sales desc
		""",
		filters,
		as_dict=True,
	)
	new = 0
	for d in data:
		d.avg_bill = flt(d.net_sales) / d.visits if d.visits else 0
		d.is_new = 1 if d.first_visit and getdate(d.first_visit) >= getdate(filters.from_date) else 0
		d.type = _("New") if d.is_new else _("Returning")
		new += d.is_new
	columns = [
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 140},
		{"fieldname": "customer_name", "label": _("Customer Name"), "fieldtype": "Data", "width": 160},
		{"fieldname": "mobile", "label": _("Phone"), "fieldtype": "Data", "width": 110},
		{"fieldname": "visits", "label": _("Visits"), "fieldtype": "Int", "width": 70},
		{"fieldname": "net_sales", "label": _("Net Sales"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "avg_bill", "label": _("Average Bill"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "first_visit", "label": _("First Visit"), "fieldtype": "Date", "width": 100},
		{"fieldname": "last_visit", "label": _("Last Visit"), "fieldtype": "Date", "width": 100},
		{"fieldname": "type", "label": _("Type"), "fieldtype": "Data", "width": 90},
	]
	summary = [
		{"label": _("Customers"), "value": len(data), "datatype": "Int"},
		{"label": _("New"), "value": new, "datatype": "Int", "indicator": "Green"},
		{"label": _("Returning"), "value": len(data) - new, "datatype": "Int", "indicator": "Blue"},
		{"label": _("Came More Than Once"), "value": sum(1 for d in data if d.visits > 1), "datatype": "Int"},
	]
	return columns, data, None, None, summary
