"""Control report: voided items, discounts, cancelled orders and cancelled invoices."""

import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.report_utils import prepare_filters


def execute(filters=None):
	filters = prepare_filters(filters)
	kind = filters.get("type") or "Voided Items"
	return {
		"Voided Items": voided_items,
		"Discounts": discounts,
		"Cancelled Orders": cancelled_orders,
		"Cancelled Invoices": cancelled_invoices,
	}[kind](filters)


def link(fieldname, label, options, width=130):
	return {"fieldname": fieldname, "label": label, "fieldtype": "Link", "options": options, "width": width}


def voided_items(filters):
	data = frappe.db.sql(
		"""
		select o.name as resto_order, date(o.opened_at) as date, o.resto_table, oi.item_code, oi.item_name,
			oi.qty, oi.amount, oi.void_reason, oi.voided_by, o.waiter,
			if(oi.station is null or oi.station = '', 0, 1) as was_sent, o.status as order_status
		from `tabResto Order Item` oi join `tabResto Order` o on o.name = oi.parent
		where oi.kitchen_status = 'Void' and o.company = %(company)s
			and date(o.opened_at) between %(from_date)s and %(to_date)s
		order by o.opened_at
		""",
		filters,
		as_dict=True,
	)
	columns = [
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		link("resto_order", _("Order"), "Resto Order"),
		link("resto_table", _("Table", context="Resto"), "Resto Table", 80),
		link("item_code", _("Item"), "Item"),
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 70},
		{"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "was_sent", "label": _("Was Cooked"), "fieldtype": "Check", "width": 90},
		{"fieldname": "void_reason", "label": _("Void Reason"), "fieldtype": "Data", "width": 180},
		link("voided_by", _("Voided By"), "User"),
		link("waiter", _("Waiter"), "User"),
		{"fieldname": "order_status", "label": _("Order Status"), "fieldtype": "Data", "width": 100},
	]
	summary = [
		{"label": _("Voided Lines"), "value": len(data), "datatype": "Int"},
		{"label": _("Voided Amount"), "value": sum(flt(d.amount) for d in data), "datatype": "Currency", "indicator": "Red"},
		{"label": _("Voided After Sending"), "value": sum(1 for d in data if d.was_sent), "datatype": "Int"},
	]
	return columns, data, None, None, summary


def discounts(filters):
	data = frappe.db.sql(
		"""
		select si.name as sales_invoice, si.posting_date as date, si.resto_order, si.resto_table,
			si.base_total as items_total, si.base_discount_amount as discount,
			if(si.base_total, si.base_discount_amount / si.base_total * 100, 0) as discount_pct,
			(select o.discount_reason from `tabResto Order` o where o.name = si.resto_order) as reason,
			ifnull(si.resto_waiter, si.owner) as waiter, si.owner as cashier
		from `tabSales Invoice` si
		where si.docstatus = 1 and si.company = %(company)s and si.base_discount_amount != 0
			and si.posting_date between %(from_date)s and %(to_date)s
		order by si.posting_date, si.name
		""",
		filters,
		as_dict=True,
	)
	columns = [
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		link("sales_invoice", _("Sales Invoice"), "Sales Invoice"),
		link("resto_order", _("Order"), "Resto Order"),
		link("resto_table", _("Table", context="Resto"), "Resto Table", 80),
		{"fieldname": "items_total", "label": _("Items Total"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "discount", "label": _("Discount"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "discount_pct", "label": _("Discount %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "reason", "label": _("Discount Reason"), "fieldtype": "Data", "width": 180},
		link("waiter", _("Waiter"), "User"),
		link("cashier", _("Cashier"), "User"),
	]
	summary = [
		{"label": _("Discounted Bills"), "value": len(data), "datatype": "Int"},
		{"label": _("Total Discount"), "value": sum(flt(d.discount) for d in data), "datatype": "Currency", "indicator": "Orange"},
	]
	return columns, data, None, None, summary


def cancelled_orders(filters):
	data = frappe.db.sql(
		"""
		select o.name as resto_order, date(o.opened_at) as date, o.order_type, o.resto_table, o.net_total,
			o.cancel_reason, o.waiter, o.modified_by as cancelled_by,
			(select count(*) from `tabResto Order Item` oi where oi.parent = o.name and ifnull(oi.station, '') != '') as sent_lines
		from `tabResto Order` o
		where o.status = 'Cancelled' and o.company = %(company)s
			and date(o.opened_at) between %(from_date)s and %(to_date)s
		order by o.opened_at
		""",
		filters,
		as_dict=True,
	)
	columns = [
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		link("resto_order", _("Order"), "Resto Order"),
		{"fieldname": "order_type", "label": _("Order Type"), "fieldtype": "Data", "width": 100},
		link("resto_table", _("Table", context="Resto"), "Resto Table", 80),
		{"fieldname": "sent_lines", "label": _("Lines Sent to Kitchen"), "fieldtype": "Int", "width": 120},
		{"fieldname": "cancel_reason", "label": _("Cancel Reason"), "fieldtype": "Data", "width": 200},
		link("waiter", _("Waiter"), "User"),
		link("cancelled_by", _("Cancelled By"), "User"),
	]
	return columns, data


def cancelled_invoices(filters):
	data = frappe.db.sql(
		"""
		select si.name as sales_invoice, si.posting_date as date, si.resto_order, si.base_grand_total as grand_total,
			si.owner as cashier, si.modified_by as cancelled_by, si.modified as cancelled_on
		from `tabSales Invoice` si
		where si.docstatus = 2 and si.company = %(company)s
			and si.posting_date between %(from_date)s and %(to_date)s
		order by si.posting_date
		""",
		filters,
		as_dict=True,
	)
	columns = [
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		link("sales_invoice", _("Sales Invoice"), "Sales Invoice"),
		link("resto_order", _("Order"), "Resto Order"),
		{"fieldname": "grand_total", "label": _("Grand Total"), "fieldtype": "Currency", "width": 120},
		link("cashier", _("Cashier"), "User"),
		link("cancelled_by", _("Cancelled By"), "User"),
		{"fieldname": "cancelled_on", "label": _("Cancelled On"), "fieldtype": "Datetime", "width": 150},
	]
	summary = [
		{"label": _("Cancelled Invoices"), "value": len(data), "datatype": "Int"},
		{"label": _("Cancelled Amount"), "value": sum(flt(d.grand_total) for d in data), "datatype": "Currency", "indicator": "Red"},
	]
	return columns, data, None, None, summary
