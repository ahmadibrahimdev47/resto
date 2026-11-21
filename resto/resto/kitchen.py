"""Kitchen tickets: sending order lines to stations, voids, statuses and printing."""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from resto.resto.routing import get_station
from resto.resto.utils import KITCHEN, MANAGER, WAITER, CASHIER, publish, require_role

TICKET_STATUSES = ("New", "In Progress", "Ready", "Served", "Cancelled")
# Order line kitchen status that follows each ticket status.
LINE_STATUS = {"New": "Sent", "In Progress": "In Progress", "Ready": "Ready", "Served": "Served"}
# Allowed moves. "New" from later states is a recall; Served -> Ready brings it back to the pass.
TRANSITIONS = {
	"New": ("In Progress", "Ready", "Served", "Cancelled"),
	"In Progress": ("Ready", "Served", "New", "Cancelled"),
	"Ready": ("Served", "New", "Cancelled"),
	"Served": ("Ready", "New"),
	"Cancelled": (),
}


def send_lines(order, course=None):
	"""Send unsent lines (not held, or of the fired course) to their stations.

	Returns the created tickets. The caller saves the order.
	"""
	lines = [
		d
		for d in order.items
		if d.kitchen_status == "Not Sent" and (not d.hold or (course and d.course == course))
	]
	if not lines:
		return []

	by_station = {}
	for d in lines:
		station = get_station(d.item_code)
		if not station:
			frappe.throw(
				_("No kitchen station makes {0}. Set a default station first.").format(frappe.bold(d.item_name))
			)
		by_station.setdefault(station, []).append(d)

	now = now_datetime()
	tickets = []
	for station, rows in by_station.items():
		ticket = _make_ticket(order, station, "Order", rows, now)
		for d in rows:
			d.kitchen_status = "Sent"
			d.hold = 0
			d.station = station
			d.ticket = ticket.name
			d.sent_at = now
		tickets.append(ticket)
	return tickets


def void_line(order, line, reason):
	"""Void one line; see void_lines."""
	tickets = void_lines(order, [line], reason)
	return tickets[0] if tickets else None


def void_lines(order, lines, reason):
	"""Void lines. Lines already sent are struck on their original ticket (a ticket left with
	nothing to make is cancelled) and each station gets one void ticket for all of them."""
	for line in lines:
		if line.kitchen_status == "Void":
			frappe.throw(_("This line is already void"))
		if flt(line.billed_qty):
			frappe.throw(_("{0} is already billed and cannot be voided").format(frappe.bold(line.item_name)))
	by_station = {}
	for line in lines:
		if line.kitchen_status != "Not Sent":
			by_station.setdefault(line.station or get_station(line.item_code), []).append(line)
			if line.ticket:
				_strike_on_ticket(line.ticket, line.name)
	now = now_datetime()
	tickets = [_make_ticket(order, station, "Void", rows, now) for station, rows in by_station.items()]
	for line in lines:
		line.kitchen_status = "Void"
		line.void_reason = reason
		line.voided_by = frappe.session.user
	return tickets


def _strike_on_ticket(ticket, order_item):
	frappe.db.sql(
		"update `tabResto Kitchen Ticket Item` set voided = 1 where parent = %s and order_item = %s", (ticket, order_item)
	)
	left = frappe.db.sql(
		"select count(*) from `tabResto Kitchen Ticket Item` where parent = %s and ifnull(voided, 0) = 0", ticket
	)[0][0]
	if not left:
		frappe.db.sql(
			"""update `tabResto Kitchen Ticket` set status = 'Cancelled'
			where name = %s and status in ('New', 'In Progress', 'Ready')""",
			ticket,
		)


def _make_ticket(order, station, ticket_type, rows, now):
	ticket = frappe.new_doc("Resto Kitchen Ticket")
	ticket.update(
		{
			"resto_order": order.name,
			"order_type": order.order_type,
			"resto_table": order.resto_table,
			"station": station,
			"ticket_type": ticket_type,
			"status": "New",
			"waiter": order.waiter or frappe.session.user,
			"sent_at": now,
		}
	)
	for d in rows:
		ticket.append(
			"items",
			{
				"order_item": d.name,
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": d.qty,
				"modifiers_text": d.modifiers_text,
				"notes": d.notes,
				"course": d.course,
			},
		)
	ticket.flags.ignore_permissions = True
	ticket.insert()
	_queue_print(ticket)
	return ticket


def _queue_print(ticket):
	printer, auto_print = frappe.db.get_value(
		"Resto Kitchen Station", ticket.station, ["printer", "auto_print"]
	) or (None, 0)
	if printer and auto_print:
		frappe.enqueue(
			"resto.resto.kitchen.print_ticket",
			ticket=ticket.name,
			queue="short",
			enqueue_after_commit=True,
		)


def print_ticket(ticket):
	"""Background job: print a ticket on its station's network printer. Never raises."""
	from resto.resto import printing

	doc = frappe.get_doc("Resto Kitchen Ticket", ticket)
	if not doc.station or not frappe.db.exists("Resto Kitchen Station", doc.station):
		return
	station = frappe.get_doc("Resto Kitchen Station", doc.station)
	if not station.printer:
		return
	try:
		printing.print_copies(
			"Resto Kitchen Ticket",
			doc.name,
			station.printer,
			station.print_format or "Resto Kitchen Ticket",
			printing.copy_labels(station.copies),
		)
		frappe.db.set_value("Resto Kitchen Ticket", doc.name, "printed", 1, update_modified=False)
	except Exception:
		frappe.log_error(title=_("Kitchen ticket {0} was not printed").format(doc.name))


def set_ticket_status(ticket_name, status):
	require_role(KITCHEN, WAITER, CASHIER)
	if status not in TICKET_STATUSES:
		frappe.throw(_("Unknown status {0}").format(status))
	if status == "Cancelled":
		require_role(MANAGER)

	ticket = frappe.get_doc("Resto Kitchen Ticket", ticket_name, for_update=True)
	if ticket.status == status:
		return ticket
	if status not in TRANSITIONS.get(ticket.status, ()):
		# A stale screen (or a ticket cancelled meanwhile) must not move it backwards.
		frappe.throw(_("Ticket {0} is {1} and cannot become {2}").format(ticket.name, _(ticket.status, context="Resto"), _(status, context="Resto")))
	now = now_datetime()
	ticket.status = status
	if status == "In Progress" and not ticket.started_at:
		ticket.started_at = now
	elif status == "Ready":
		ticket.started_at = ticket.started_at or now
		ticket.ready_at = ticket.ready_at if ticket.served_at else now
		ticket.served_at = None
	elif status == "Served":
		ticket.ready_at = ticket.ready_at or now
		ticket.served_at = now
	elif status == "New":
		# Recall: back on the screen.
		ticket.ready_at = ticket.served_at = None
	ticket.flags.ignore_permissions = True
	ticket.save()

	if ticket.ticket_type == "Order" and status in LINE_STATUS and ticket.resto_order:
		line_names = [d.order_item for d in ticket.items if d.order_item]
		if line_names:
			# One conditional update: a void that commits meanwhile is never overwritten.
			frappe.db.sql(
				"""update `tabResto Order Item` set kitchen_status = %s
				where name in %s and kitchen_status != 'Void'""",
				(LINE_STATUS[status], line_names),
			)
	publish("ticket", ticket=ticket.name, station=ticket.station, order=ticket.resto_order, status=status)
	return ticket


def get_tickets(station=None, include_done_minutes=15):
	"""Open tickets for the kitchen display (oldest first), plus recently finished ones."""
	require_role(KITCHEN, WAITER, CASHIER)
	fields = [
		"name", "resto_order", "order_type", "resto_table", "station", "ticket_type", "status", "waiter",
		"sent_at", "started_at", "ready_at", "served_at",
	]
	cond = {"station": station} if station else {}
	now = now_datetime()
	# Work to do first (void tickets on top, then oldest), then what waits for the waiter.
	todo = frappe.get_all(
		"Resto Kitchen Ticket", filters={**cond, "status": ("in", ("New", "In Progress"))}, fields=fields,
		order_by="sent_at asc", limit=300,
	)
	todo.sort(key=lambda t: (t.ticket_type != "Void", t.sent_at))
	# Ready food nobody marked Served leaves the board after two hours.
	ready = frappe.get_all(
		"Resto Kitchen Ticket",
		filters={**cond, "status": "Ready", "ready_at": (">=", frappe.utils.add_to_date(now, hours=-2))},
		fields=fields, order_by="ready_at asc", limit=100,
	)
	done_from = frappe.utils.add_to_date(now, minutes=-int(include_done_minutes or 0))
	done = frappe.get_all(
		"Resto Kitchen Ticket", filters={**cond, "status": "Served", "served_at": (">=", done_from)}, fields=fields,
		order_by="served_at desc", limit=30,
	)
	tickets = todo + ready + done
	items = {}
	if tickets:
		for d in frappe.get_all(
			"Resto Kitchen Ticket Item",
			filters={"parent": ("in", [t.name for t in tickets]), "parenttype": "Resto Kitchen Ticket"},
			fields=["parent", "item_name", "qty", "modifiers_text", "notes", "course", "voided"],
			order_by="idx asc",
		):
			items.setdefault(d.parent, []).append(d)

	result = []
	for t in tickets:
		t.items = items.get(t.name, [])
		result.append(
			{
				"name": t.name,
				"order": t.resto_order,
				"order_type": t.order_type,
				"table": t.resto_table,
				"station": t.station,
				"ticket_type": t.ticket_type,
				"status": t.status,
				"waiter": frappe.utils.get_fullname(t.waiter) if t.waiter else "",
				"sent_at": t.sent_at,
				"started_at": t.started_at,
				"ready_at": t.ready_at,
				"served_at": t.served_at,
				"items": [
					{
						"item_name": d.item_name,
						"qty": d.qty,
						"modifiers_text": d.modifiers_text,
						"notes": d.notes,
						"course": d.course,
						"voided": d.voided,
					}
					for d in t.items
				],
			}
		)
	return result


# --------------------------------------------------------------------------- kitchen display API


@frappe.whitelist()
def kds_data(station=None):
	from resto.resto.utils import get_settings

	return {
		"stations": frappe.get_all("Resto Kitchen Station", filters={"is_active": 1}, fields=["name", "color"], order_by="name"),
		"tickets": get_tickets(station or None),
		"refresh_seconds": frappe.utils.cint(get_settings().screen_refresh_seconds) or 15,
		"now": now_datetime(),
	}


@frappe.whitelist(methods=["POST"])
def kds_set_status(ticket, status):
	set_ticket_status(ticket, status)
	return status


@frappe.whitelist(methods=["POST"])
def kds_print(ticket):
	require_role(KITCHEN, WAITER, CASHIER)
	if not frappe.db.get_value("Resto Kitchen Station", frappe.db.get_value("Resto Kitchen Ticket", ticket, "station"), "printer"):
		frappe.throw(_("This station has no printer"))
	frappe.enqueue("resto.resto.kitchen.print_ticket", ticket=ticket, queue="short", enqueue_after_commit=True)
	return ticket
