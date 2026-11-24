"""Kitchen tickets: sending order lines to stations, voids, statuses and printing."""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, getdate, now_datetime

from resto.resto.routing import get_station
from resto.resto.utils import KITCHEN, MANAGER, WAITER, CASHIER, publish, require_role

# How a station's tickets are printed (Resto Kitchen Station.print_method).
SERVER_PRINTER = "Server Printer"
BROWSER_AUTO = "Browser Automatic"
BROWSER_BUTTON = "Browser Button"
NO_PRINTING = "No Printing"
AUTO_PRINT_MINUTES = 30
# Only tickets still to be made print by themselves; one already made from the screen is not printed late.
TO_PRINT_STATUSES = ("New", "In Progress")
# A screen confirms each ticket it printed; a claim not confirmed by then is offered again (as a reprint).
CLAIM_SECONDS = 90
# A screen printing automatically checks in on every refresh; without it the kitchen screens warn.
PRINTER_SEEN_SECONDS = 120
# Kitchen screens warn about a ticket of a self-printing station still not printed after this...
UNPRINTED_SECONDS = 60
# ...when it was sent in the last hours.
ALERT_HOURS = 12
# Station ticket numbers are kept this long after their business day.
DAILY_NO_KEEP_SECONDS = 2 * 24 * 3600
# Hides the print page banner and the page margins, whatever the browser remembers.
BROWSER_PAGE_STYLE = "<style>@page { margin: 0; } .action-banner, .print-hide { display: none !important; }</style>"

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
	for station, rows in sorted(by_station.items()):
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
	tickets = [_make_ticket(order, station, "Void", rows, now) for station, rows in sorted(by_station.items())]
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
			# Order tickets only: a void refers to the number of the ticket it cancels.
			"daily_no": _daily_no(station, now) if ticket_type == "Order" else None,
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
				"sides_text": d.get("sides_text"),
				"notes": d.notes,
				"course": d.course,
			},
		)
	ticket.flags.ignore_permissions = True
	ticket.insert()
	_queue_print(ticket)
	if ticket_type == "Order":
		try:
			_warn_if_nobody_prints(station)
		except Exception:
			pass  # a warning only: it never stops a send
	return ticket


def _warn_if_nobody_prints(station):
	"""The waiter is told at once when the ticket will not come out on paper by itself."""
	if frappe.db.get_value("Resto Kitchen Station", station, "print_method") != BROWSER_AUTO:
		return
	if frappe.cache.get_value(_printer_key(station)):
		return
	frappe.msgprint(
		_("No kitchen screen is printing for {0} now: tell the kitchen to turn on Auto Printing.").format(
			frappe.bold(station)
		),
		indicator="orange",
		alert=True,
	)


def _daily_no(station, now):
	"""The next ticket number of the station in this business day (Resto Settings "Business Day Starts
	At"), to match paper with the screen and the receipt.

	An atomic counter in the cache, not a read of the tickets: two sends at the same moment never share
	a number, and a number never locks a row or holds up a send or a payment. The counter starts from
	the tickets of the day when the cache has none (a restart). A send that fails leaves a gap. Without
	the cache the ticket simply has no number."""
	from resto.resto.demand import day_start_hour

	start = get_datetime(getdate(now)).replace(hour=day_start_hour())
	if now < start:
		start = add_to_date(start, days=-1)
	key = frappe.cache.make_key(f"resto_kds_no:{station}:{start.date()}")
	try:
		if frappe.cache.get(key) is None:
			last = frappe.db.sql(
				"""select ifnull(max(daily_no), 0) from `tabResto Kitchen Ticket`
				where station = %s and sent_at >= %s""",
				(station, start),
			)[0][0]
			# Only the first of two sends at the same moment sets it.
			frappe.cache.set(key, cint(last), ex=DAILY_NO_KEEP_SECONDS, nx=True)
		return cint(frappe.cache.incr(key))
	except Exception:
		frappe.log_error(title=_("Kitchen ticket number not given"))
		return None


def _queue_print(ticket):
	"""Server printing only; a station that prints from the browser is printed by its kitchen screen."""
	method, printer, auto_print = frappe.db.get_value(
		"Resto Kitchen Station", ticket.station, ["print_method", "printer", "auto_print"]
	) or (None, None, 0)
	if method == SERVER_PRINTER and printer and auto_print:
		frappe.enqueue(
			"resto.resto.kitchen.print_ticket",
			ticket=ticket.name,
			queue="short",
			enqueue_after_commit=True,
		)
		# A Print press meanwhile is a reprint.
		frappe.db.set_value("Resto Kitchen Ticket", ticket.name, "print_claimed_at", now_datetime(), update_modified=False)


def print_ticket(ticket, reprint=0):
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
			extra={"resto_reprint": cint(reprint)},
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
		"sent_at", "started_at", "ready_at", "served_at", "printed", "daily_no",
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
			fields=["parent", "item_name", "qty", "modifiers_text", "sides_text", "notes", "course", "voided", "order_item"],
			order_by="idx asc",
		):
			items.setdefault(d.parent, []).append(d)

	# Void tickets: which of their dishes have no waste posted yet (the kitchen can post it if cooked).
	void_lines = [d.order_item for t in tickets if t.ticket_type == "Void" for d in items.get(t.name, []) if d.order_item]
	no_waste = set()
	if void_lines:
		no_waste = set(
			frappe.get_all(
				"Resto Order Item",
				filters={"name": ("in", void_lines), "kitchen_status": "Void", "void_waste": ("is", "not set")},
				pluck="name",
			)
		)
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
				"printed": cint(t.printed),
				"daily_no": t.daily_no,
				"items": [
					{
						"item_name": d.item_name,
						"qty": d.qty,
						"modifiers_text": d.modifiers_text,
						"sides_text": d.sides_text,
						"notes": d.notes,
						"course": d.course,
						"voided": d.voided,
						"order_item": d.order_item,
						"waste_open": 1 if d.order_item in no_waste else 0,
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

	stations = frappe.get_all(
		"Resto Kitchen Station", filters={"is_active": 1}, fields=["name", "color", "print_method"], order_by="name"
	)
	return {
		"stations": stations,
		"print_methods": {s.name: s.print_method or BROWSER_BUTTON for s in stations},
		"tickets": get_tickets(station or None),
		"print_alerts": print_alerts(),
		"refresh_seconds": frappe.utils.cint(get_settings().screen_refresh_seconds) or 15,
		"now": now_datetime(),
	}


@frappe.whitelist(methods=["POST"])
def kds_set_status(ticket, status):
	set_ticket_status(ticket, status)
	return status


@frappe.whitelist(methods=["POST"])
def kds_print(ticket):
	"""The Print button of a ticket, whatever the station's printing method: on the station's network
	printer, or the pages for the browser to print (one per copy). A ticket printed before is marked
	as a reprint, so the kitchen does not make it twice."""
	require_role(KITCHEN, WAITER, CASHIER)
	station = _ticket_station(ticket)
	# Locked: a press on another device, or a screen printing automatically, waits and then sees a reprint.
	printed, claimed = frappe.db.get_value(
		"Resto Kitchen Ticket", ticket, ["printed", "print_claimed_at"], for_update=True
	) or (0, None)
	reprint = 1 if (cint(printed) or claimed) else 0
	if (station.print_method or BROWSER_BUTTON) == SERVER_PRINTER and station.printer:
		frappe.enqueue(
			"resto.resto.kitchen.print_ticket", ticket=ticket, reprint=reprint, queue="short", enqueue_after_commit=True
		)
		result = {"server": 1}
	else:
		result = {"pages": ticket_pages(ticket, station, reprint=reprint)}
	frappe.db.set_value("Resto Kitchen Ticket", ticket, "print_claimed_at", now_datetime(), update_modified=False)
	return result


@frappe.whitelist(methods=["POST"])
def kds_printed(ticket):
	"""The browser printed the ticket's pages (given by kds_print or kds_claim_print, which stamp the
	claim)."""
	require_role(KITCHEN, WAITER, CASHIER)
	if not frappe.db.get_value("Resto Kitchen Ticket", ticket, "print_claimed_at"):
		frappe.throw(_("Ticket {0} was not given to print").format(ticket))
	frappe.db.set_value("Resto Kitchen Ticket", ticket, "printed", 1, update_modified=False)
	return True


@frappe.whitelist(methods=["POST"])
def kds_claim_print(stations):
	"""For the kitchen screen that prints by itself for the given stations (set to print automatically
	from the browser): the next ticket to print, or None.

	The ticket is claimed (locked, claim time stamped), so a second screen never prints it as well. The
	screen confirms with kds_printed once the browser printed it; a claim not confirmed within
	CLAIM_SECONDS (the computer restarted, the network dropped) is offered again, marked as a reprint."""
	require_role(KITCHEN)
	names = [s for s in (frappe.parse_json(stations) if isinstance(stations, str) else stations or []) if s]
	auto = frappe.get_all(
		"Resto Kitchen Station",
		filters={"name": ("in", names or [""]), "is_active": 1, "print_method": BROWSER_AUTO},
		fields=["name", "auto_print_minutes"],
	)
	now = now_datetime()
	for s in auto:
		frappe.cache.set_value(_printer_key(s.name), str(now), expires_in_sec=PRINTER_SEEN_SECONDS)
	try:
		return _claim_next(auto, now)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		# Another screen or a send holds the ticket (or changed it since this request began): nothing
		# is claimed, the next poll tries again.
		frappe.db.rollback()
		return None


def _claim_next(auto, now):
	skip = set()
	for _round in range(20):
		name = _next_to_print(auto, now, skip)
		if not name:
			return None
		skip.add(name)
		row = frappe.db.get_value(
			"Resto Kitchen Ticket",
			name,
			["printed", "print_claimed_at", "status", "ticket_type", "station"],
			as_dict=True,
			for_update=True,
		)
		claimed_at = now_datetime()
		if not row or cint(row.printed) or row.status not in TO_PRINT_STATUSES:
			continue
		if row.print_claimed_at and row.print_claimed_at > add_to_date(claimed_at, seconds=-CLAIM_SECONDS):
			continue  # another screen is printing it
		if row.ticket_type == "Void" and not _original_on_paper(name):
			# Nothing of it reached the kitchen on paper: the struck lines show on the screen only.
			frappe.db.set_value("Resto Kitchen Ticket", name, "printed", 1, update_modified=False)
			continue
		# Rendered before it is claimed: a ticket that could not be rendered is not marked as a reprint
		# when it finally prints. The row lock keeps other screens off it meanwhile.
		pages = _render_for_claim(name, row)
		if pages is None:
			continue
		frappe.db.set_value("Resto Kitchen Ticket", name, "print_claimed_at", claimed_at, update_modified=False)
		return {"name": name, "pages": pages}
	return None


def _render_for_claim(name, row):
	"""The ticket's pages, or None when it cannot be rendered (a broken custom format): it must not hold
	up the others, it is tried again on the next poll and the kitchen screens show it unprinted."""
	messages = len(frappe.local.message_log or [])
	try:
		station = frappe.get_cached_doc("Resto Kitchen Station", row.station)
		return ticket_pages(name, station, reprint=1 if row.print_claimed_at else 0)
	except Exception:
		# The error is not shown on the kitchen screen at every poll: it is logged once in five minutes.
		if frappe.local.message_log:
			del frappe.local.message_log[messages:]
		key = f"resto_kds_render_failed:{name}"
		if not frappe.cache.get_value(key):
			frappe.cache.set_value(key, 1, expires_in_sec=300)
			frappe.log_error(title=_("Kitchen ticket {0} was not printed").format(name))
		return None


def _next_to_print(stations, now, skip):
	"""The oldest ticket of the stations not printed, not claimed in the last CLAIM_SECONDS, still to
	be made (a ticket already made from the screen is not printed late), sent within its station's
	window (old tickets never print by themselves)."""
	free_since = add_to_date(now, seconds=-CLAIM_SECONDS)
	best = None
	for s in stations:
		since = add_to_date(now, minutes=-(cint(s.auto_print_minutes) or AUTO_PRINT_MINUTES))
		rows = frappe.db.sql(
			"""select name, sent_at from `tabResto Kitchen Ticket`
			where station = %(station)s and ifnull(printed, 0) = 0 and status in %(statuses)s
				and sent_at >= %(since)s and (print_claimed_at is null or print_claimed_at < %(free)s)
				and name not in %(skip)s
			order by sent_at asc, name asc limit 1""",
			{
				"station": s.name,
				"statuses": TO_PRINT_STATUSES,
				"since": since,
				"free": free_since,
				"skip": tuple(skip) or ("",),
			},
			as_dict=True,
		)
		if rows and (best is None or rows[0].sent_at < best.sent_at):
			best = rows[0]
	return best.name if best else None


def _original_on_paper(void_ticket):
	"""Whether a ticket struck by this void ticket was printed (or is being printed)."""
	originals = frappe.db.sql_list(
		"""select distinct oi.ticket from `tabResto Kitchen Ticket Item` ti
		join `tabResto Order Item` oi on oi.name = ti.order_item
		where ti.parent = %s and ifnull(oi.ticket, '') != ''""",
		void_ticket,
	)
	if not originals:
		return True
	return bool(
		frappe.db.sql(
			"""select count(*) from `tabResto Kitchen Ticket`
			where name in %s and (ifnull(printed, 0) = 1 or print_claimed_at is not null) for update""",
			(tuple(originals),),
		)[0][0]
	)


def void_sent_at(order_item):
	"""When the void ticket of an order line was sent (None when it has none)."""
	return frappe.db.sql(
		"""select max(t.sent_at) from `tabResto Kitchen Ticket Item` ti
		join `tabResto Kitchen Ticket` t on t.name = ti.parent
		where t.ticket_type = 'Void' and ti.order_item = %s""",
		order_item,
	)[0][0]


def _printer_key(station):
	return f"resto_kds_printer:{station}"


def print_alerts():
	"""What every kitchen screen shows about printing:
	- unprinted: tickets of the stations that print by themselves still not printed a minute after they
	  were sent (printer off, no paper, no screen printing, or sent before the station's window);
	- no_screen: stations printing automatically from the browser that no kitchen screen prints for."""
	stations = frappe.get_all(
		"Resto Kitchen Station",
		filters={"is_active": 1, "print_method": ("in", (SERVER_PRINTER, BROWSER_AUTO))},
		fields=["name", "print_method", "auto_print"],
	)
	auto = [s.name for s in stations if s.print_method == BROWSER_AUTO or cint(s.auto_print)]
	if not auto:
		return {"unprinted": [], "no_screen": []}
	now = now_datetime()
	unprinted = frappe.get_all(
		"Resto Kitchen Ticket",
		filters={
			"station": ("in", auto),
			"printed": 0,
			"status": ("in", TO_PRINT_STATUSES),
			"sent_at": ("between", (add_to_date(now, hours=-ALERT_HOURS), add_to_date(now, seconds=-UNPRINTED_SECONDS))),
		},
		fields=["name", "station", "resto_table", "resto_order", "order_type", "ticket_type", "sent_at", "daily_no"],
		order_by="sent_at asc",
		limit=30,
	)
	no_screen = [
		s.name for s in stations if s.print_method == BROWSER_AUTO and not frappe.cache.get_value(_printer_key(s.name))
	]
	return {"unprinted": unprinted, "no_screen": no_screen}


def _ticket_station(ticket):
	station = frappe.db.get_value("Resto Kitchen Ticket", ticket, "station")
	if not station or not frappe.db.exists("Resto Kitchen Station", station):
		frappe.throw(_("Ticket {0} has no kitchen station").format(ticket))
	return frappe.get_cached_doc("Resto Kitchen Station", station)


def ticket_pages(ticket, station, reprint=0):
	"""The ticket's print page once per copy of its station, each with its copy label, as the
	browser prints them (the print format in its own language and direction)."""
	from resto.resto import printing

	pages = []
	# The caller checked the kitchen roles; a User Permission on a table or a user must not stop a ticket.
	ignore = frappe.flags.ignore_print_permissions
	frappe.flags.ignore_print_permissions = True
	try:
		for label in printing.copy_labels(station.copies):
			doc = frappe.get_doc("Resto Kitchen Ticket", ticket)
			doc.resto_copy_label = label
			doc.resto_reprint = cint(reprint)
			html = frappe.get_print(
				"Resto Kitchen Ticket", ticket, station.print_format or "Resto Kitchen Ticket", doc=doc, no_letterhead=1
			)
			pages.append(html.replace("</head>", BROWSER_PAGE_STYLE + "</head>", 1))
	finally:
		frappe.flags.ignore_print_permissions = ignore
	return pages
