"""Server API for the floor screen (waiters, cashiers, hosts). Every call checks roles."""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

from resto.resto import billing, kitchen
from resto.resto.doctype.resto_modifier_group.resto_modifier_group import groups_for_item
from resto.resto.doctype.resto_order.resto_order import ACTIVE, default_service_percent
from resto.resto.utils import (
	CASHIER,
	HOST,
	KITCHEN,
	MANAGER,
	WAITER,
	get_settings,
	has_role,
	publish,
	require_role,
)

STAFF = (WAITER, CASHIER, HOST)


def _loads(value, default=None):
	if isinstance(value, str):
		return json.loads(value) if value else default
	return value if value is not None else default


def _get_order(name, statuses=ACTIVE):
	"""Load and lock an order for change."""
	order = frappe.get_doc("Resto Order", name, for_update=True)
	if statuses and order.status not in statuses:
		frappe.throw(_("Order {0} is {1}").format(order.name, _(order.status)))
	return order


def _save(order):
	order.flags.ignore_permissions = True
	order.flags.resto_api = True
	order.save()
	return order


def _line(order, line_name):
	row = next((d for d in order.items if d.name == line_name), None)
	if not row:
		frappe.throw(_("Line not found on order {0}").format(order.name))
	return row


def _reopen_if_billed(order):
	"""Changing a bill-requested order sends it back to Open; the printed bill is no longer valid."""
	if order.status == "Bill Requested":
		order.status = "Open"
		order.bill_requested_at = None
		order.bill_details = None
		order.bill_service_amount = order.bill_tax_amount = order.bill_grand_total = 0


# --------------------------------------------------------------------------- reading


@frappe.whitelist()
def get_floor():
	require_role(*STAFF)
	settings = get_settings()
	now = now_datetime()
	hold = cint(settings.reservation_hold_minutes) or 30

	orders = frappe.get_all(
		"Resto Order",
		filters={"status": ("in", ACTIVE)},
		fields=[
			"name",
			"order_type",
			"resto_table",
			"status",
			"guests",
			"opened_at",
			"net_total",
			"waiter",
			"customer_name",
			"phone",
			"delivery_status",
			"bill_grand_total",
			"rebill",
		],
		order_by="opened_at asc",
	)
	ready = (
		dict(
			frappe.db.sql(
				"""select parent, count(*) from `tabResto Order Item`
				where parenttype='Resto Order' and kitchen_status='Ready' and parent in %s group by parent""",
				[[o.name for o in orders]],
			)
		)
		if orders
		else {}
	)
	for o in orders:
		o.ready_count = ready.get(o.name, 0)
		o.waiter_name = frappe.utils.get_fullname(o.waiter) if o.waiter else ""
		o.minutes = int((now - get_datetime(o.opened_at)).total_seconds() // 60) if o.opened_at else 0

	# An order whose invoice was cancelled after the guests left is listed apart, not on the table.
	by_table = {o.resto_table: o for o in orders if o.resto_table and not o.rebill}

	reservations = frappe.get_all(
		"Resto Reservation",
		filters={"reservation_date": nowdate(), "status": "Booked", "resto_table": ("is", "set")},
		fields=["name", "resto_table", "customer_name", "reservation_time", "guests"],
	)
	reserved = {}
	for r in reservations:
		at = get_datetime(f"{r.reservation_date if r.get('reservation_date') else nowdate()} {r.reservation_time}")
		if add_to_date(at, minutes=-hold) <= now <= add_to_date(at, minutes=hold):
			reserved.setdefault(r.resto_table, r)

	halls = []
	for hall in frappe.get_all(
		"Resto Hall", filters={"is_active": 1}, fields=["name", "grid_columns"], order_by="sequence asc, name asc"
	):
		tables = frappe.get_all(
			"Resto Table",
			filters={"hall": hall.name, "is_active": 1},
			fields=["name", "seats", "shape", "grid_row", "grid_col"],
			order_by="grid_row asc, grid_col asc, name asc",
		)
		for t in tables:
			order = by_table.get(t.name)
			t.order = order
			t.reservation = reserved.get(t.name)
			if order:
				t.status = "bill" if order.status == "Bill Requested" else "occupied"
			elif t.reservation:
				t.status = "reserved"
			else:
				t.status = "free"
		halls.append({"name": hall.name, "grid_columns": cint(hall.grid_columns) or 6, "tables": tables})

	return {
		"halls": halls,
		"orders": [o for o in orders if o.order_type != "Dine In" or o.rebill],
		"refresh_seconds": cint(settings.screen_refresh_seconds) or 15,
		"roles": {
			"manager": has_role(),
			"waiter": has_role(WAITER),
			"cashier": has_role(CASHIER),
			"host": has_role(HOST),
		},
		"service_percent": {
			t: default_service_percent(settings, t) for t in ("Dine In", "Takeaway", "Delivery")
		},
		"delivery_fee": flt(settings.delivery_fee),
	}


@frappe.whitelist()
def get_menu():
	require_role(*STAFF)
	settings = get_settings()
	price_list = settings.selling_price_list or (
		frappe.db.get_value("POS Profile", settings.pos_profile, "selling_price_list") if settings.pos_profile else None
	)
	if not price_list:
		frappe.throw(_("Set a POS Profile or a Selling Price List in Resto Settings first"))

	today = getdate()
	prices = {}
	for p in frappe.get_all(
		"Item Price",
		filters={"price_list": price_list, "selling": 1},
		fields=["item_code", "price_list_rate", "customer", "valid_from", "valid_upto"],
		order_by="valid_from asc",
	):
		if p.customer or (p.valid_from and getdate(p.valid_from) > today) or (p.valid_upto and getdate(p.valid_upto) < today):
			continue
		prices[p.item_code] = flt(p.price_list_rate)

	allowed_groups = None
	if settings.menu_item_group:
		lft, rgt = frappe.db.get_value("Item Group", settings.menu_item_group, ["lft", "rgt"])
		allowed_groups = set(
			frappe.get_all("Item Group", filters={"lft": (">=", lft), "rgt": ("<=", rgt)}, pluck="name")
		)

	items = []
	if prices:
		for it in frappe.get_all(
			"Item",
			filters={"name": ("in", list(prices)), "disabled": 0, "is_sales_item": 1, "has_variants": 0},
			fields=["name", "item_name", "item_group", "image"],
			order_by="item_name asc",
		):
			if allowed_groups is not None and it.item_group not in allowed_groups:
				continue
			items.append(
				{
					"item_code": it.name,
					"item_name": it.item_name,
					"item_group": it.item_group,
					"image": it.image,
					"rate": prices[it.name],
					"has_modifiers": bool(groups_for_item(it.name)),
				}
			)
	groups = sorted({i["item_group"] for i in items})
	return {"groups": groups, "items": items, "price_list": price_list}


@frappe.whitelist()
def get_modifiers(item_code):
	require_role(*STAFF)
	return groups_for_item(item_code)


@frappe.whitelist()
def get_order(order):
	require_role(*STAFF, KITCHEN)
	doc = frappe.get_doc("Resto Order", order)
	data = doc.as_dict()
	data["waiter_name"] = frappe.utils.get_fullname(doc.waiter) if doc.waiter else ""
	data["invoiced_total"] = doc.invoiced_total()
	return data


@frappe.whitelist()
def search_customers(txt=""):
	require_role(*STAFF)
	txt = f"%{txt}%"
	return frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		or_filters={"name": ("like", txt), "customer_name": ("like", txt), "mobile_no": ("like", txt)},
		fields=["name", "customer_name", "mobile_no"],
		limit=20,
	)


# --------------------------------------------------------------------------- orders


@frappe.whitelist(methods=["POST"])
def create_order(order_type="Dine In", table=None, guests=1, customer=None, customer_name=None, phone=None, address=None):
	require_role(WAITER, CASHIER)
	order = frappe.new_doc("Resto Order")
	order.update(
		{
			"order_type": order_type,
			"resto_table": table if order_type == "Dine In" else None,
			"guests": cint(guests) or 1,
			"customer": customer,
			"customer_name": customer_name or (frappe.db.get_value("Customer", customer, "customer_name") if customer else None),
			"phone": phone,
			"address": address,
			"delivery_status": "Pending" if order_type == "Delivery" else None,
		}
	)
	order.flags.ignore_permissions = True
	order.insert()
	return order.name


@frappe.whitelist(methods=["POST"])
def update_order_info(order, guests=None, customer=None, customer_name=None, phone=None, address=None, notes=None):
	require_role(WAITER, CASHIER, HOST)
	doc = _get_order(order)
	for field, value in (
		("customer", customer),
		("customer_name", customer_name),
		("phone", phone),
		("address", address),
		("notes", notes),
	):
		if value is not None:
			doc.set(field, value or None)
	if guests is not None:
		doc.guests = cint(guests) or 1
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def add_items(order, items):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	for it in _loads(items, []):
		qty = flt(it.get("qty") or 1)
		doc.append(
			"items",
			{
				"item_code": it.get("item_code"),
				"qty": qty,
				"notes": it.get("notes"),
				"course": it.get("course") or None,
				"hold": cint(it.get("hold")),
				"modifiers": json.dumps(it.get("modifiers") or []),
				"kitchen_status": "Not Sent",
				"billed_qty": 0,
			},
		)
	_reopen_if_billed(doc)
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def update_line(order, line, qty=None, notes=None, course=None, hold=None):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	row = _line(doc, line)
	if row.kitchen_status != "Not Sent":
		frappe.throw(_("{0} was sent to the kitchen; void it and add it again").format(frappe.bold(row.item_name)))
	if qty is not None:
		row.qty = flt(qty)
	if notes is not None:
		row.notes = notes
	if course is not None:
		row.course = course or None
	if hold is not None:
		row.hold = cint(hold)
	_reopen_if_billed(doc)
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def remove_line(order, line):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	row = _line(doc, line)
	if row.kitchen_status != "Not Sent":
		frappe.throw(_("{0} was sent to the kitchen; void it instead").format(frappe.bold(row.item_name)))
	doc.remove(row)
	_reopen_if_billed(doc)
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def send_to_kitchen(order):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	tickets = kitchen.send_lines(doc)
	_save(doc)
	return [t.name for t in tickets]


@frappe.whitelist(methods=["POST"])
def fire_course(order, course):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	tickets = kitchen.send_lines(doc, course=course)
	_save(doc)
	return [t.name for t in tickets]


@frappe.whitelist(methods=["POST"])
def void_line(order, line, reason, cooked=0):
	"""Void a line (manager). cooked: the dish was already made, so its ingredients are booked as
	waste; otherwise they would stay in book stock and show up later as a count loss."""
	require_role(MANAGER)
	if not (reason or "").strip():
		frappe.throw(_("Enter a reason for the void"))
	doc = _get_order(order)
	row = _line(doc, line)
	was_sent = row.kitchen_status not in ("Not Sent", "Void")
	kitchen.void_line(doc, row, reason.strip())
	_reopen_if_billed(doc)
	_save(doc)
	doc.add_comment("Info", _("Voided {0}: {1}").format(row.item_name, reason))
	if cint(cooked) and was_sent:
		_waste_for_void(doc, row, reason.strip())
	return doc.name


def _waste_for_void(order, row, reason):
	items = [{"item_code": row.item_code, "qty": flt(row.qty), "reason": "Other", "note": reason}]
	for mod in json.loads(row.modifiers or "[]"):
		if flt(mod.get("price")) and mod.get("item"):
			items.append({"item_code": mod["item"], "qty": flt(row.qty), "reason": "Other", "note": reason})
	frappe.db.savepoint("resto_void_waste")
	try:
		waste = frappe.get_doc(
			{
				"doctype": "Resto Waste",
				"remarks": _("Voided after cooking on order {0}: {1}").format(order.name, reason),
				"items": items,
			}
		)
		waste.flags.ignore_permissions = True
		waste.insert()
		waste.submit()
		order.add_comment("Info", _("Waste {0} posted for the voided {1}").format(waste.name, row.item_name))
	except Exception:
		# The void stands; the waste can be entered by hand.
		frappe.db.rollback(save_point="resto_void_waste")
		frappe.clear_messages()
		frappe.log_error(title=f"Resto void waste for {order.name}")
		frappe.msgprint(_("The item is void, but its waste could not be posted; enter it in Resto Waste."), indicator="orange")


@frappe.whitelist(methods=["POST"])
def mark_served(order):
	"""Waiter served everything that is ready on this order."""
	require_role(WAITER, CASHIER)
	for name in frappe.get_all(
		"Resto Kitchen Ticket",
		filters={"resto_order": order, "status": "Ready", "ticket_type": "Order"},
		pluck="name",
	):
		kitchen.set_ticket_status(name, "Served")
	return order


@frappe.whitelist(methods=["POST"])
def move_table(order, table):
	require_role(WAITER, CASHIER, HOST)
	doc = _get_order(order)
	if doc.order_type != "Dine In":
		frappe.throw(_("Only dine-in orders have a table"))
	old = doc.resto_table
	doc.resto_table = table
	_save(doc)
	frappe.db.sql(
		"""update `tabResto Kitchen Ticket` set resto_table=%s
		where resto_order=%s and status in ('New', 'In Progress', 'Ready')""",
		(table, doc.name),
	)
	doc.add_comment("Info", _("Moved from table {0} to {1}").format(old, table))
	publish("order", order=doc.name, table=old)
	return doc.name


@frappe.whitelist(methods=["POST"])
def merge_orders(target, source):
	"""Move every line of `source` into `target` (kitchen state kept) and close `source`."""
	require_role(WAITER, CASHIER)
	if target == source:
		frappe.throw(_("Choose two different orders"))
	# Lock both orders in name order so two merges of the same pair cannot deadlock.
	first, second = sorted((target, source))
	locked = {first: _get_order(first), second: _get_order(second)}
	tgt, src = locked[target], locked[source]
	if any(not d.cancelled for d in src.invoices) or any(not d.cancelled for d in tgt.invoices):
		frappe.throw(_("Orders with a paid part cannot be merged"))
	if src.company != tgt.company or src.pos_profile != tgt.pos_profile:
		frappe.throw(_("Orders of different POS Profiles cannot be merged"))
	if flt(src.discount_amount):
		# The merged order keeps only its own discount; nothing may disappear silently.
		frappe.throw(_("Order {0} has a discount; remove it or give it on this order before merging").format(src.name))

	offset = len(tgt.items)
	frappe.db.sql(
		"""update `tabResto Order Item` set parent=%s, idx=idx+%s
		where parent=%s and parenttype='Resto Order'""",
		(tgt.name, offset, src.name),
	)
	frappe.db.sql(
		"""update `tabResto Kitchen Ticket` set resto_order=%s, resto_table=%s
		where resto_order=%s""",
		(tgt.name, tgt.resto_table, src.name),
	)
	src.reload()
	src.status = "Merged"
	src.merged_into = tgt.name
	src.closed_at = now_datetime()
	src.discount_amount = src.discount_percentage = 0
	_save(src)

	tgt.reload()
	tgt.guests = cint(tgt.guests) + cint(src.guests)
	_reopen_if_billed(tgt)
	_save(tgt)
	tgt.add_comment("Info", _("Merged order {0} into this order").format(src.name))
	return tgt.name


@frappe.whitelist(methods=["POST"])
def apply_discount(order, percentage=0, amount=0, reason=None):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	doc.discount_percentage = flt(percentage)
	doc.discount_amount = 0 if flt(percentage) else flt(amount)
	doc.discount_reason = reason
	_reopen_if_billed(doc)
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def set_service_charge(order, percent):
	require_role(MANAGER)
	doc = _get_order(order)
	doc.service_charge_percent = flt(percent)
	_reopen_if_billed(doc)
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def set_delivery(order, delivery_status=None, driver=None, delivery_fee=None):
	require_role(CASHIER)
	doc = _get_order(order, statuses=None)
	if doc.order_type != "Delivery":
		frappe.throw(_("This is not a delivery order"))
	if delivery_status is not None:
		doc.delivery_status = delivery_status
	if driver is not None:
		doc.driver = driver or None
	if delivery_fee is not None:
		if any(not d.cancelled for d in doc.invoices):
			frappe.throw(_("The delivery fee is already billed"))
		doc.delivery_fee = flt(delivery_fee)
	doc.flags.resto_allow_closed = True
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def cancel_order(order, reason):
	"""Cancel an order. With sent items this needs a manager and the kitchen gets void tickets."""
	require_role(WAITER, CASHIER)
	if not (reason or "").strip():
		frappe.throw(_("Enter a reason"))
	doc = _get_order(order)
	if any(not d.cancelled for d in doc.invoices):
		frappe.throw(_("Part of this order is paid; it cannot be cancelled"))
	sent = [d for d in doc.items if d.kitchen_status not in ("Not Sent", "Void")]
	if sent:
		require_role(MANAGER)
	kitchen.void_lines(doc, sent, reason)
	for d in doc.items:
		if d.kitchen_status == "Not Sent":
			d.kitchen_status = "Void"
			d.void_reason = reason
			d.voided_by = frappe.session.user
	doc.status = "Cancelled"
	doc.cancel_reason = reason
	# Nothing is left to discount (a fixed discount would be more than the zero total).
	doc.discount_amount = doc.discount_percentage = 0
	doc.closed_at = now_datetime()
	_save(doc)
	if doc.reservation:
		frappe.db.set_value("Resto Reservation", doc.reservation, "status", "Cancelled")
	return doc.name


# --------------------------------------------------------------------------- bill


@frappe.whitelist()
def get_bill(order, lines=None):
	require_role(WAITER, CASHIER)
	doc = frappe.get_doc("Resto Order", order)
	return billing.bill_preview(doc, _loads(lines))


@frappe.whitelist(methods=["POST"])
def request_bill(order):
	"""Freeze the bill for printing; the table shows 'bill requested'."""
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	unsent = [d.item_name for d in doc.active_lines() if d.kitchen_status == "Not Sent"]
	if unsent:
		frappe.throw(_("Send these items to the kitchen or remove them first: {0}").format(", ".join(unsent)))
	preview = billing.bill_preview(doc)
	doc.status = "Bill Requested"
	doc.bill_requested_at = now_datetime()
	doc.bill_service_amount = preview["service"]
	doc.bill_tax_amount = preview["tax_total"]
	doc.bill_grand_total = preview["payable"]
	doc.bill_details = json.dumps(preview, default=str, ensure_ascii=False)
	_save(doc)
	return preview


@frappe.whitelist(methods=["POST"])
def settle(order, payments, lines=None, expected=None, request_id=None):
	require_role(CASHIER)
	# A retried click after the last payment closed the order gets the same invoice back.
	doc = _get_order(order, statuses=None if request_id else ACTIVE)
	if doc.status not in ACTIVE:
		return _settle_result(doc, billing.settle(doc, [], request_id=request_id))
	if doc.order_type != "Dine In" and any(d.kitchen_status == "Not Sent" for d in doc.active_lines()):
		# Takeaway and delivery can be paid first; the kitchen still gets the order.
		kitchen.send_lines(doc, course=None)
		for d in doc.items:
			if d.kitchen_status == "Not Sent" and d.hold:
				d.hold = 0
		kitchen.send_lines(doc)
		_save(doc)
		doc = _get_order(order)
	si = billing.settle(
		doc, _loads(payments, []), _loads(lines), expected=None if expected in (None, "") else flt(expected), request_id=request_id
	)
	return _settle_result(doc, si)


def _settle_result(doc, si):
	return {
		"sales_invoice": si.name,
		"grand_total": si.grand_total,
		"rounded_total": si.rounded_total,
		"paid_amount": si.paid_amount,
		"change_amount": si.change_amount,
		"status": frappe.db.get_value("Resto Order", doc.name, "status"),
		"print_format": frappe.db.get_value("POS Profile", doc.pos_profile, "print_format"),
	}


# --------------------------------------------------------------------------- reservations


@frappe.whitelist(methods=["POST"])
def seat_reservation(reservation, table=None):
	require_role(HOST, WAITER)
	res = frappe.get_doc("Resto Reservation", reservation, for_update=True)
	if res.status != "Booked":
		frappe.throw(_("Reservation {0} is {1}").format(res.name, _(res.status)))
	table = table or res.resto_table
	if not table:
		frappe.throw(_("Choose a table"))
	order = frappe.new_doc("Resto Order")
	order.update(
		{
			"order_type": "Dine In",
			"resto_table": table,
			"guests": res.guests,
			"customer": res.customer,
			"customer_name": res.customer_name,
			"phone": res.phone,
		}
	)
	order.flags.ignore_permissions = True
	order.insert()
	order.reservation = res.name
	_save(order)
	res.status = "Seated"
	res.resto_table = table
	res.resto_order = order.name
	res.flags.ignore_permissions = True
	res.save()
	return order.name
