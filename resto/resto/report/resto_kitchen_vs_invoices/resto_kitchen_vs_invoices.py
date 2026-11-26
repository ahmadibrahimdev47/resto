"""Control report: every dish sent to the kitchen against what was billed for it.

One row per order line sent to the kitchen (and per line billed without being sent). Its result says
whether it was billed, is still on an open order, was on an order cancelled after cooking, was voided
after it was sent (who, when, why, and the waste booked for it), or does not match.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, getdate, now_datetime

from resto.resto.report_utils import prepare_filters

BILLED = "Billed in Full"
OPEN = "Not Billed: Order Open"
CANCELLED = "Sent, Order Cancelled"
UNBILLED_CLOSED = "Not Billed: Order Closed"
VOIDED = "Voided After Sending"
OVER = "Billed More Than Sent"
NOT_SENT = "Billed, Not Sent to the Kitchen"
# A staff meal (D-231): no bill, taken from stock at cost by its Stock Entry.
STAFF_MEAL = "Staff Meal, Posted to Stock"
EXCEPTIONS = (OPEN, CANCELLED, UNBILLED_CLOSED, VOIDED, OVER, NOT_SENT)
COLORS = {BILLED: "green", STAFF_MEAL: "blue", OPEN: "orange", VOIDED: "orange", CANCELLED: "red", UNBILLED_CLOSED: "red", OVER: "red", NOT_SENT: "red"}


def execute(filters=None):
	filters = prepare_filters(filters)
	period = _period(filters)
	lines = _lines(filters, period)
	invoices = _invoices([l.resto_order for l in lines])
	voids = _void_times([l.name for l in lines if l.kitchen_status == "Void"])
	waste = _waste_costs([l.void_waste for l in lines if l.get("void_waste")])

	rows = []
	for l in lines:
		inv = [i for i in invoices.get(l.resto_order, []) if l.name in i.lines]
		if filters.get("cashier") and not any(i.paid_by == filters.cashier for i in invoices.get(l.resto_order, [])):
			continue  # an order this cashier billed nothing of
		l.result = _result(l)
		# Sent and not paid for: on an open order, a cancelled one, or a closed one (a mismatch).
		unpaid = l.result in (OPEN, CANCELLED, UNBILLED_CLOSED)
		l.unbilled_qty = max(flt(l.qty) - flt(l.billed_qty), 0) if unpaid else 0
		l.unbilled_amount = flt(l.amount) / flt(l.qty) * l.unbilled_qty if flt(l.qty) else 0
		l.extras_amount = flt(l.sides_amount) * flt(l.qty)
		l.sales_invoice = ", ".join(i.sales_invoice for i in inv)
		l.cashier = ", ".join(sorted({i.paid_by for i in inv if i.paid_by}))
		l.voided_at = voids.get(l.name)
		w = waste.get(l.get("void_waste")) if l.get("void_waste") else None
		l.waste_cost = w.cost if w else 0
		# Posted later, or by someone else than who voided it (the kitchen or the cashier): shown.
		l.waste_by = w.owner if w else None
		l.waste_at = w.creation if w else None
		l.result_label = _(l.result)
		l.result_color = COLORS.get(l.result)
		rows.append(l)

	data = rows if filters.get("show") == "All Lines" else [l for l in rows if l.result in EXCEPTIONS]
	return _columns(), data, None, None, _summary(rows)


def _period(filters):
	"""(start, end) datetimes: the shift's, or the date range's."""
	if filters.get("shift"):
		shift = frappe.db.get_value(
			"Resto Shift", filters.shift, ["opening_time", "closing_time", "pos_profile", "name"], as_dict=True
		)
		if not shift:
			frappe.throw(_("Shift {0} was not found").format(filters.shift))
		filters.pos_profile = filters.get("pos_profile") or shift.pos_profile
		end = shift.closing_time
		return get_datetime(shift.opening_time), get_datetime(end) if end else now_datetime()
	return get_datetime(getdate(filters.from_date)), get_datetime(f"{getdate(filters.to_date)} 23:59:59.999999")


def _lines(filters, period):
	cond = ""
	if filters.get("station"):
		cond += " and oi.station = %(station)s"
	if filters.get("pos_profile"):
		cond += " and o.pos_profile = %(pos_profile)s"
	return frappe.db.sql(
		f"""
		select oi.name, oi.parent as resto_order, o.status as order_status, o.order_type, o.resto_table,
			o.waiter, o.pos_profile, oi.item_code, oi.item_name, oi.qty, oi.amount, oi.billed_qty,
			oi.kitchen_status, oi.station, oi.ticket, oi.sent_at,
			(select t.daily_no from `tabResto Kitchen Ticket` t where t.name = oi.ticket) as daily_no,
			oi.modifiers_text, oi.sides_text, oi.sides_amount, oi.void_reason, oi.voided_by, oi.void_waste,
			o.cancel_reason, o.staff_meal
		from `tabResto Order Item` oi join `tabResto Order` o on o.name = oi.parent
		where o.company = %(company)s and o.status != 'Merged'
			and (
				(ifnull(oi.ticket, '') != '' and oi.sent_at between %(start)s and %(end)s)
				or (ifnull(oi.ticket, '') = '' and oi.billed_qty > 0 and o.opened_at between %(start)s and %(end)s)
			)
			{cond}
		order by oi.sent_at, o.name, oi.idx
		""",
		{**filters, "start": period[0], "end": period[1]},
		as_dict=True,
	)


def _result(l):
	if not l.ticket:
		return NOT_SENT
	if l.order_status == "Cancelled" and not (l.kitchen_status == "Void" and l.void_reason != l.cancel_reason):
		# Cancelling an order voids its sent lines; they were made for nobody who paid. A line voided on
		# its own before (its own reason) stays a void.
		return CANCELLED
	if l.kitchen_status == "Void":
		return VOIDED
	if l.staff_meal and l.order_status == "Posted":
		return STAFF_MEAL
	billed, qty = flt(l.billed_qty), flt(l.qty)
	if billed > qty:
		return OVER
	if billed >= qty:
		return BILLED
	if l.order_status in ("Open", "Bill Requested"):
		return OPEN
	if l.order_status == "Cancelled":
		return CANCELLED
	return UNBILLED_CLOSED


def _invoices(orders):
	"""{order: [invoice rows not cancelled, with the lines each billed]}."""
	out = {}
	if not orders:
		return out
	for r in frappe.db.sql(
		"""select parent, sales_invoice, paid_by, `lines` from `tabResto Order Invoice`
		where parenttype = 'Resto Order' and parent in %s and ifnull(cancelled, 0) = 0""",
		(tuple(set(orders)),),
		as_dict=True,
	):
		r.lines = set(json.loads(r.lines or "{}"))
		out.setdefault(r.parent, []).append(r)
	return out


def _void_times(lines):
	"""{order line: when its void ticket was sent}."""
	if not lines:
		return {}
	return dict(
		frappe.db.sql(
			"""select ti.order_item, min(t.sent_at) from `tabResto Kitchen Ticket Item` ti
			join `tabResto Kitchen Ticket` t on t.name = ti.parent
			where t.ticket_type = 'Void' and ti.order_item in %s group by ti.order_item""",
			(tuple(lines),),
		)
	)


def _waste_costs(names):
	"""Cost of each waste booked for a void, from the stock ledger of its Material Issue."""
	if not names:
		return {}
	return {
		name: frappe._dict(cost=flt(cost), owner=owner, creation=creation)
		for name, cost, owner, creation in frappe.db.sql(
			"""select w.name, sum(-sle.stock_value_difference), w.owner, w.creation from `tabResto Waste` w
			join `tabStock Ledger Entry` sle on sle.voucher_type = 'Stock Entry' and sle.voucher_no = w.stock_entry
				and sle.is_cancelled = 0
			where w.docstatus = 1 and w.name in %s group by w.name, w.owner, w.creation""",
			(tuple(set(names)),),
		)
	}


def _columns():
	def col(fieldname, label, fieldtype="Data", width=110, options=None):
		c = {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "width": width}
		if options:
			c["options"] = options
		return c

	return [
		col("result_label", _("Result"), width=190),
		col("sent_at", _("Sent At"), "Datetime", 150),
		col("resto_order", _("Order"), "Link", 130, "Resto Order"),
		col("resto_table", _("Table", context="Resto"), "Link", 80, "Resto Table"),
		col("order_status", _("Order Status"), width=100),
		col("station", _("Station"), "Link", 100, "Resto Kitchen Station"),
		col("ticket", _("Ticket"), "Link", 140, "Resto Kitchen Ticket"),
		col("daily_no", _("Station Number"), "Int", 70),
		col("item_code", _("Item"), "Link", 120, "Item"),
		col("item_name", _("Item Name"), width=150),
		col("qty", _("Sent Qty"), "Float", 80),
		col("billed_qty", _("Billed Qty"), "Float", 80),
		col("unbilled_qty", _("Not Billed Qty"), "Float", 90),
		col("amount", _("Amount"), "Currency", 100),
		col("unbilled_amount", _("Not Billed Amount"), "Currency", 110),
		col("sales_invoice", _("Sales Invoice"), width=150),
		col("cashier", _("Cashier"), width=130),
		col("modifiers_text", _("Modifiers"), width=140),
		col("sides_text", _("Sides"), width=160),
		col("extras_amount", _("Extra Portions Amount"), "Currency", 110),
		col("voided_at", _("Voided At"), "Datetime", 150),
		col("voided_by", _("Voided By"), "Link", 130, "User"),
		col("void_reason", _("Void Reason"), width=170),
		col("void_waste", _("Waste Entry"), "Link", 130, "Resto Waste"),
		col("waste_cost", _("Waste Cost"), "Currency", 100),
		col("waste_by", _("Waste Posted By"), "Link", 130, "User"),
		col("waste_at", _("Waste Posted At"), "Datetime", 150),
		col("cancel_reason", _("Cancel Reason"), width=160),
		col("waiter", _("Waiter"), "Link", 130, "User"),
	]


def _summary(data):
	"""Over every line of the filters, whatever rows are shown."""

	def amount(result):
		return sum(flt(d.unbilled_amount) for d in data if d.result == result)

	voided = [d for d in data if d.result in (VOIDED, CANCELLED)]
	out = [
		{"label": _("Not Billed: Order Open"), "value": amount(OPEN), "datatype": "Currency", "indicator": "Orange"},
		{"label": _("Sent, Order Cancelled"), "value": amount(CANCELLED), "datatype": "Currency", "indicator": "Red"},
		{
			"label": _("Mismatches"),
			"value": sum(1 for d in data if d.result in (UNBILLED_CLOSED, OVER, NOT_SENT)),
			"datatype": "Int",
			"indicator": "Red",
		},
		{
			"label": _("Voided After Sending"),
			"value": sum(flt(d.amount) for d in data if d.result == VOIDED),
			"datatype": "Currency",
			"indicator": "Orange",
		},
		{"label": _("Waste of Voids"), "value": sum(flt(d.waste_cost) for d in voided), "datatype": "Currency", "indicator": "Orange"},
		{
			"label": _("Voids Without Waste"),
			"value": sum(1 for d in voided if not d.get("void_waste")),
			"datatype": "Int",
			"indicator": "Blue",
		},
	]
	out[:0] = [
		{"label": _("Sent Qty"), "value": sum(flt(d.qty) for d in data if d.ticket), "datatype": "Float"},
		{"label": _("Billed Qty"), "value": sum(flt(d.billed_qty) for d in data), "datatype": "Float", "indicator": "Green"},
	]
	out.append({"label": _("Changed Sides"), "value": sum(1 for d in data if d.sides_text), "datatype": "Int"})
	out.append({"label": _("Extra Portions Amount"), "value": sum(flt(d.extras_amount) for d in data), "datatype": "Currency"})
	return out
