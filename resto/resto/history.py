"""The floor's invoice history, shift history and the open shift's figures (D-212): read only."""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

from resto.resto.utils import CASHIER, MANAGER, require_role

PAGE = 20

STATUSES = ("paid", "unpaid", "partly", "owed", "overdue", "returns")
ORDER_TYPES = ("Dine In", "Takeaway", "Delivery")


def _day_start():
	"""The start of this business day (Resto Settings "Business Day Starts At")."""
	from resto.resto.demand import day_start_hour

	now = now_datetime()
	start = get_datetime(getdate(now)).replace(hour=day_start_hour())
	return start if now >= start else add_to_date(start, days=-1)


def _period(period):
	"""(from, to) creation times for a period, or (None, None) for any day."""
	start = _day_start()
	if period == "today":
		return start, None
	if period == "yesterday":
		return add_to_date(start, days=-1), start
	if period == "week":
		return add_to_date(start, days=-6), None
	if period == "month":
		return add_to_date(start, days=-29), None
	return None, None


@frappe.whitelist()
def invoices(txt=None, period=None, status=None, start=0, page_length=PAGE, shift=None, order_type=None, mode=None):
	"""Restaurant invoices for the floor's history, newest first, 20 at a time. txt: invoice or order
	number, customer or phone, on any day; otherwise today by default. period: today / yesterday / week
	/ month / any. status: paid / unpaid / partly / owed / overdue / returns. shift: "current" for the
	user's open shift (none open: no rows) or a shift name. order_type: Dine In / Takeaway / Delivery.
	mode: a payment method the bill was paid with (in part). A cashier sees every restaurant invoice
	(they have read on Sales Invoice), so a guest's bill from another shift can be found and reprinted.
	A return (credit note) carries no order of its own: it is the restaurant's through the invoice it
	returns."""
	require_role(CASHIER)
	txt = (txt or "").strip()
	if shift == "current":
		from resto.resto.shift import open_shift_of

		shift = open_shift_of(frappe.session.user)
		if not shift:
			return {"invoices": [], "more": False}
	period = period or ("any" if txt or shift else "today")
	order = "coalesce(nullif(si.resto_order, ''), nullif(orig.resto_order, ''))"
	cond = ["si.docstatus = 1", f"{order} is not null"]
	values = {}
	since, until = _period(period)
	if since:
		cond.append("si.creation >= %(since)s")
		values["since"] = since
	if until:
		cond.append("si.creation < %(until)s")
		values["until"] = until
	if shift:
		cond.append("si.resto_shift = %(shift)s")
		values["shift"] = shift
	if order_type in ORDER_TYPES:
		cond.append("ro.order_type = %(order_type)s")
		values["order_type"] = order_type
	if mode:
		cond.append(
			"""exists (select 1 from `tabSales Invoice Payment` p where p.parent = si.name
			and p.parenttype = 'Sales Invoice' and p.mode_of_payment = %(mode)s and p.amount != 0)"""
		)
		values["mode"] = mode
	if status == "returns":
		cond.append("si.is_return = 1")
	elif status in STATUSES:
		cond.append("si.is_return = 0")
		# By what is still owed against the bill total: a debt paid later by a Payment Entry leaves
		# paid_amount as it was at the counter.
		total = "(case when si.disable_rounded_total = 1 then si.grand_total else coalesce(nullif(si.rounded_total, 0), si.grand_total) end)"
		if status == "paid":
			cond.append("si.outstanding_amount <= 0")
		elif status == "unpaid":
			cond.append(f"si.outstanding_amount > 0 and si.outstanding_amount >= {total} - 0.005")
		elif status == "partly":
			cond.append(f"si.outstanding_amount > 0 and si.outstanding_amount < {total} - 0.005")
		elif status == "owed":
			cond.append("si.outstanding_amount > 0")
		elif status == "overdue":
			cond.append("si.outstanding_amount > 0 and si.due_date < %(today)s")
			values["today"] = nowdate()
	if txt:
		from resto.resto.floor import _phone

		values["txt"] = f"%{txt}%"
		phone = _phone(txt)
		like = ["si.name like %(txt)s", f"{order} like %(txt)s", "si.customer_name like %(txt)s", "si.customer like %(txt)s"]
		if phone and len(phone) >= 4:
			values["phone"] = f"%{phone}%"
			like.append("ro.phone like %(phone)s")
		cond.append("(" + " or ".join(like) + ")")
	where = " and ".join(cond)
	values.update({"start": max(cint(start), 0), "limit": min(max(cint(page_length), 0) or PAGE, 100)})
	rows = frappe.db.sql(
		f"""select si.name, si.posting_date, si.posting_time, si.customer, si.customer_name, si.grand_total,
			si.rounded_total, si.disable_rounded_total, si.paid_amount, si.change_amount, si.outstanding_amount,
			si.is_return, si.return_against, si.status, si.due_date, {order} as resto_order, ro.resto_table,
			ro.order_type as resto_order_type, si.resto_shift, si.owner, ro.phone
		from `tabSales Invoice` si
		left join `tabSales Invoice` orig on si.is_return = 1 and orig.name = si.return_against
		left join `tabResto Order` ro on ro.name = {order}
		where {where}
		order by si.creation desc
		limit %(limit)s offset %(start)s""",
		values,
		as_dict=True,
	)
	names = [r.name for r in rows]
	modes = {}
	if names:
		for p in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ("in", names), "parenttype": "Sales Invoice", "amount": ("!=", 0)},
			fields=["parent", "mode_of_payment"],
			order_by="idx",
		):
			modes.setdefault(p.parent, [])
			if p.mode_of_payment not in modes[p.parent]:
				modes[p.parent].append(p.mode_of_payment)
	for r in rows:
		r.total = flt(r.grand_total) if cint(r.disable_rounded_total) else flt(r.rounded_total or r.grand_total)
		r.modes = modes.get(r.name, [])
		r.cashier = frappe.utils.get_fullname(r.owner)
		r.overdue = int(bool(not r.is_return and flt(r.outstanding_amount) > 0 and r.due_date and getdate(r.due_date) < getdate()))
	return {"invoices": rows, "more": len(rows) == values["limit"]}


@frappe.whitelist()
def counts():
	"""The counts on the invoice list's tabs: the user's open shift, this business day, the bills still
	owed (any day) and today's returns; and the payment methods to filter by."""
	require_role(CASHIER)
	from resto.resto.shift import _modes, open_shift_of
	from resto.resto.utils import get_settings

	order = "coalesce(nullif(si.resto_order, ''), nullif(orig.resto_order, ''))"
	base = f"""select count(*) from `tabSales Invoice` si
		left join `tabSales Invoice` orig on si.is_return = 1 and orig.name = si.return_against
		where si.docstatus = 1 and {order} is not null"""
	since = _day_start()
	shift = open_shift_of(frappe.session.user)
	one = lambda cond, values=None: cint(frappe.db.sql(f"{base} and {cond}", values or {})[0][0])  # noqa: E731
	return {
		"shift": one("si.resto_shift = %(shift)s", {"shift": shift}) if shift else None,
		"today": one("si.creation >= %(since)s", {"since": since}),
		"owed": one("si.is_return = 0 and si.outstanding_amount > 0"),
		"returns": one("si.is_return = 1 and si.creation >= %(since)s", {"since": since}),
		"modes": [m.mode_of_payment for m in _modes(get_settings().pos_profile)] if get_settings().pos_profile else [],
	}


@frappe.whitelist()
def invoice(name):
	"""One restaurant invoice for the list's side pane: its lines, taxes, payments and change."""
	require_role(CASHIER)
	doc = frappe.get_doc("Sales Invoice", name)
	order = doc.get("resto_order") or (
		doc.return_against and frappe.db.get_value("Sales Invoice", doc.return_against, "resto_order")
	)
	if doc.docstatus != 1 or not order:
		frappe.throw(_("{0} is not a restaurant invoice").format(name))
	ro = frappe.db.get_value("Resto Order", order, ["resto_table", "order_type", "phone"], as_dict=True) or {}
	total = flt(doc.grand_total) if cint(doc.disable_rounded_total) else flt(doc.rounded_total or doc.grand_total)
	return {
		"name": doc.name,
		"customer": doc.customer,
		"customer_name": doc.customer_name,
		"posting_date": doc.posting_date,
		"posting_time": doc.posting_time,
		"cashier": frappe.utils.get_fullname(doc.owner),
		"resto_order": order,
		"resto_table": ro.get("resto_table"),
		"order_type": ro.get("order_type"),
		"phone": ro.get("phone"),
		"is_return": doc.is_return,
		"return_against": doc.return_against,
		"status": doc.status,
		"items": [
			{"item_name": d.item_name, "qty": d.qty, "rate": d.rate, "amount": d.amount}
			for d in doc.items
			if not d.get("resto_packaging")  # no price, not on the guest's bill (D-217)
		],
		"taxes": [{"description": t.description, "amount": t.tax_amount_after_discount_amount} for t in doc.taxes if flt(t.tax_amount_after_discount_amount)],
		"net_total": doc.net_total,
		"total": doc.total,
		"discount_amount": doc.discount_amount,
		"grand_total": total,
		"payments": [{"mode_of_payment": p.mode_of_payment, "amount": p.amount} for p in doc.payments if flt(p.amount)],
		"change_amount": doc.change_amount,
		"outstanding_amount": doc.outstanding_amount,
		"printed": cint(doc.get("resto_receipt_printed")),
		"can_return": int(_manager() and not doc.is_return and frappe.has_permission("Sales Invoice", "create")),
	}


@frappe.whitelist()
def shifts(start=0, page_length=PAGE):
	"""Shift history: the cashier's own shifts, or everyone's for a manager, newest first, to see a
	closing again and print it."""
	require_role(CASHIER)
	filters = {} if _manager() else {"user": frappe.session.user}
	rows = frappe.get_all(
		"Resto Shift",
		filters=filters,
		fields=[
			"name", "user", "pos_profile", "status", "opening_time", "closing_time", "sales_count", "net_sales",
			"collected", "expenses_total", "total_difference",
		],
		order_by="opening_time desc",
		start=max(cint(start), 0),
		page_length=min(max(cint(page_length), 0) or PAGE, 100),
	)
	for r in rows:
		r.user_name = frappe.utils.get_fullname(r.user)
		if r.status != "Closed":
			# An open shift's figures are the floor's live ones (closing_data), not these.
			for f in ("sales_count", "net_sales", "collected", "expenses_total", "total_difference"):
				r[f] = None
	return rows


def _manager():
	return bool({MANAGER, "System Manager"} & set(frappe.get_roles()))


@frappe.whitelist()
def live():
	"""The cashier's open shift as it stands, for the floor's top bar: since when and how many bills.
	Money only when the drawer is not counted blind (Resto Settings), or for a manager: a running
	total would tell a blind count what to find."""
	require_role(CASHIER)
	from resto.resto import shift as shifts

	name = shifts.open_shift_of(frappe.session.user)
	if not name:
		return None
	doc = frappe.get_doc("Resto Shift", name)
	s = shifts.compute(doc)["summary"]
	out = {"shift": name, "since": doc.opening_time, "sales_count": s["sales_count"], "returns_count": s["returns_count"]}
	if not shifts._blind() or _manager():
		for k in ("net_sales", "collected", "outstanding", "expenses_total", "net_cash", "debts_count", "debts_total"):
			out[k] = s[k]
	return out
