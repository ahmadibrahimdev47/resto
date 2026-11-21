import frappe
from frappe import _
from frappe.utils import flt

from resto.resto.report_utils import food_sales, pct, prepare_filters, waste_cost_by_doc

GROUPS = {"Item": "wi.item_code", "Item Group": "i.item_group", "Reason": "wi.reason", "Day": "w.posting_date"}


def execute(filters=None):
	filters = prepare_filters(filters)
	group_by = filters.get("group_by") or "Item"
	# Row amounts are stored at submit; scale each document to its stock ledger value, which a
	# back-dated purchase can repost.
	ledger = waste_cost_by_doc(filters)
	rows = frappe.db.sql(
		f"""
		select {GROUPS[group_by]} as `group`, w.name as waste, w.total_amount, wi.item_name, wi.qty, wi.uom, wi.amount
		from `tabResto Waste Item` wi
		join `tabResto Waste` w on w.name = wi.parent
		join `tabItem` i on i.name = wi.item_code
		where w.docstatus = 1 and w.company = %(company)s and w.posting_date between %(from_date)s and %(to_date)s
		""",
		filters,
		as_dict=True,
	)
	groups = {}
	for r in rows:
		factor = flt(ledger.get(r.waste)) / flt(r.total_amount) if flt(r.total_amount) else 1
		g = groups.setdefault(r.group, frappe._dict(group=r.group, item_name=r.item_name, qty=0, uom=r.uom, amount=0, docs=set()))
		g.qty += flt(r.qty)
		g.amount += flt(r.amount) * factor
		g.docs.add(r.waste)
	data = list(groups.values())
	for d in data:
		d.entries = len(d.pop("docs"))
	data.sort(key=(lambda d: str(d.group)) if group_by == "Day" else (lambda d: -d.amount))
	sales = food_sales(filters)
	total = sum(flt(d.amount) for d in data)
	for d in data:
		d.share = pct(d.amount, total)
		d.of_sales = pct(d.amount, sales)
		if group_by == "Reason":
			d.group = _(d.group, context="Resto")

	columns = [{"fieldname": "group", "label": _(group_by, context="Resto"), "fieldtype": "Data", "width": 150}]
	if group_by == "Item":
		columns += [
			{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170},
			{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 80},
			{"fieldname": "uom", "label": _("UOM"), "fieldtype": "Data", "width": 70},
		]
	columns += [
		{"fieldname": "entries", "label": _("Entries"), "fieldtype": "Int", "width": 80},
		{"fieldname": "amount", "label": _("Cost"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "share", "label": _("Share %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "of_sales", "label": _("% of Net Sales"), "fieldtype": "Percent", "width": 110},
	]
	summary = [
		{"label": _("Waste Cost"), "value": total, "datatype": "Currency", "indicator": "Orange"},
		{"label": _("% of Net Sales"), "value": pct(total, sales), "datatype": "Percent"},
	]
	chart = {
		"data": {"labels": [str(d.group) for d in data][:20], "datasets": [{"name": _("Cost"), "values": [flt(d.amount) for d in data][:20]}]},
		"type": "pie" if group_by == "Reason" else "bar",
	}
	return columns, data, None, chart, summary
