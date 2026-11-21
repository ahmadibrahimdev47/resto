import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.report_utils import invoice_condition, prepare_filters, service_account

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

INVOICE_GROUPS = {
	"Day": "si.posting_date",
	"Hour": "hour(si.posting_time)",
	"Weekday": "dayofweek(si.posting_date)",
	"Month": "date_format(si.posting_date, '%%Y-%%m')",
	"Order Type": "ifnull(nullif(si.resto_order_type, ''), 'Counter')",
	"Table": "ifnull(si.resto_table, '')",
	"Hall": "ifnull((select t.hall from `tabResto Table` t where t.name = si.resto_table), '')",
	"Waiter": "ifnull(si.resto_waiter, si.owner)",
	"Cashier": "si.owner",
	"POS Profile": "ifnull(si.pos_profile, '')",
}
ITEM_GROUPS = {"Item": "sii.item_code", "Item Group": "sii.item_group"}


def execute(filters=None):
	filters = prepare_filters(filters)
	filters.service_account = service_account()
	group_by = filters.get("group_by") or "Day"
	if group_by in ITEM_GROUPS:
		columns, data = by_item(filters, group_by)
	elif group_by == "Payment Method":
		columns, data = by_payment(filters)
	else:
		columns, data = by_invoice(filters, group_by)
	chart = {
		"data": {
			"labels": [str(d["group"]) for d in data][:60],
			"datasets": [{"name": _("Net Sales"), "values": [flt(d.get("net_sales")) for d in data][:60]}],
		},
		"type": "bar",
	}
	return columns, data, None, chart, summary(data)


def group_column(group_by):
	return {"fieldname": "group", "label": _(group_by, context="Resto"), "fieldtype": "Data", "width": 160}


def cur(fieldname, label, width=120):
	return {"fieldname": fieldname, "label": label, "fieldtype": "Currency", "width": width}


def by_invoice(filters, group_by):
	expr = INVOICE_GROUPS[group_by]
	rows = frappe.db.sql(
		f"""
		select {expr} as `group`,
			sum(case when si.is_return = 0 then 1 else 0 end) as bills,
			sum(case when si.is_return = 1 then 1 else 0 end) as returns,
			sum(ifnull(si.resto_guests, 0)) as guests,
			sum(si.base_total) as gross_sales,
			sum(si.base_discount_amount) as discount,
			sum(si.base_net_total) as net_sales,
			sum(ifnull((select sum(t.base_tax_amount_after_discount_amount) from `tabSales Taxes and Charges` t
				where t.parent = si.name and t.parenttype = 'Sales Invoice' and t.account_head = %(service_account)s), 0)) as service,
			sum(si.base_total_taxes_and_charges) as all_taxes,
			sum(if(si.base_rounded_total != 0, si.base_rounded_total, si.base_grand_total)) as grand_total,
			sum(if(ifnull(si.resto_order, '') != '',
				if(si.base_rounded_total != 0, si.base_rounded_total, si.base_grand_total), 0)) as resto_total
		from `tabSales Invoice` si
		where {invoice_condition()}
		group by `group`
		order by `group`
		""",
		filters,
		as_dict=True,
	)
	for r in rows:
		r.taxes = flt(r.all_taxes) - flt(r.service)
		r.avg_bill = flt(r.grand_total) / r.bills if r.bills else 0
		# Only restaurant orders carry a guest count; counter sales are left out of the per-guest figure.
		r.avg_guest = flt(r.resto_total) / r.guests if r.guests else 0
		if group_by == "Weekday" and r.group:
			r.group = _(WEEKDAYS[int(r.group) - 1])
		elif group_by == "Hour" and r.group is not None:
			r.group = f"{int(r.group):02d}:00"
		elif group_by == "Order Type":
			r.group = _(r.group, context="Resto")
		elif group_by in ("Waiter", "Cashier"):
			r.group = frappe.utils.get_fullname(r.group)
	columns = [
		group_column(group_by),
		{"fieldname": "bills", "label": _("Bills"), "fieldtype": "Int", "width": 80},
		{"fieldname": "returns", "label": _("Returns"), "fieldtype": "Int", "width": 80},
		{"fieldname": "guests", "label": _("Guests"), "fieldtype": "Int", "width": 80},
		cur("gross_sales", _("Items Total")),
		cur("discount", _("Discount")),
		cur("net_sales", _("Net Sales")),
		cur("service", _("Service Charge")),
		cur("taxes", _("Taxes")),
		cur("grand_total", _("Total Collected")),
		cur("avg_bill", _("Average Bill")),
		cur("avg_guest", _("Average per Guest")),
	]
	return columns, rows


def by_item(filters, group_by):
	expr = ITEM_GROUPS[group_by]
	rows = frappe.db.sql(
		f"""
		select {expr} as `group`, max(sii.item_name) as item_name,
			sum(sii.stock_qty) as qty, sum(sii.base_amount) as gross_sales,
			sum(sii.base_net_amount) as net_sales, count(distinct si.name) as bills
		from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
		where {invoice_condition()}
		group by `group`
		order by net_sales desc
		""",
		filters,
		as_dict=True,
	)
	total = sum(flt(r.net_sales) for r in rows)
	for r in rows:
		r.share = flt(r.net_sales) / total * 100 if total else 0
		r.avg_price = flt(r.net_sales) / flt(r.qty) if flt(r.qty) else 0
	columns = [group_column(group_by)]
	if group_by == "Item":
		columns.append({"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 180})
	columns += [
		{"fieldname": "qty", "label": _("Qty Sold"), "fieldtype": "Float", "width": 100},
		{"fieldname": "bills", "label": _("Bills"), "fieldtype": "Int", "width": 80},
		cur("gross_sales", _("Items Total")),
		cur("net_sales", _("Net Sales")),
		cur("avg_price", _("Average Price")),
		{"fieldname": "share", "label": _("Share %"), "fieldtype": "Percent", "width": 90},
	]
	return columns, rows


def by_payment(filters):
	rows = frappe.db.sql(
		f"""
		select si.name, sip.mode_of_payment, sip.type, sip.base_amount, si.base_change_amount
		from `tabSales Invoice Payment` sip join `tabSales Invoice` si on si.name = sip.parent
		where {invoice_condition()} and sip.base_amount != 0
		order by si.name, sip.idx
		""",
		filters,
		as_dict=True,
	)
	totals, change_done = {}, set()
	for r in rows:
		amount = flt(r.base_amount)
		if r.type == "Cash" and r.name not in change_done:
			# Change is given back in cash.
			amount -= flt(r.base_change_amount)
			change_done.add(r.name)
		t = totals.setdefault(r.mode_of_payment, {"group": r.mode_of_payment, "bills": set(), "net_sales": 0})
		t["bills"].add(r.name)
		t["net_sales"] += amount
	data = []
	total = sum(t["net_sales"] for t in totals.values())
	for t in sorted(totals.values(), key=lambda x: -x["net_sales"]):
		data.append(
			{"group": t["group"], "bills": len(t["bills"]), "net_sales": t["net_sales"], "share": t["net_sales"] / total * 100 if total else 0}
		)
	columns = [
		group_column("Payment Method"),
		{"fieldname": "bills", "label": _("Bills"), "fieldtype": "Int", "width": 80},
		cur("net_sales", _("Collected"), 140),
		{"fieldname": "share", "label": _("Share %"), "fieldtype": "Percent", "width": 90},
	]
	return columns, data


def summary(data):
	if not data or "grand_total" not in data[0]:
		return None
	net = sum(flt(d.get("net_sales")) for d in data)
	bills = sum(flt(d.get("bills")) for d in data)
	guests = sum(flt(d.get("guests")) for d in data)
	grand = sum(flt(d.get("grand_total")) for d in data)
	return [
		{"label": _("Net Sales"), "value": net, "datatype": "Currency", "indicator": "Blue"},
		{"label": _("Bills"), "value": bills, "datatype": "Int"},
		{"label": _("Guests"), "value": guests, "datatype": "Int"},
		{"label": _("Average Bill"), "value": grand / bills if bills else 0, "datatype": "Currency"},
		{"label": _("Average per Guest"), "value": grand / guests if guests else 0, "datatype": "Currency"},
	]
