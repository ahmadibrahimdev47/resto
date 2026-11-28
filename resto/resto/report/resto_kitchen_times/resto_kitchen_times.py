import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, time_diff_in_seconds

from resto.resto.report_utils import prepare_filters


def spans(t, now, late):
	"""(wait, cook, total, pickup, late) of one ticket, in minutes. A time set from the floor's kitchen
	state buttons (D-293) is when someone tapped, not when the kitchen worked: a span that starts or
	ends on it is left out (None), and whether the ticket was late is not known (None): it counts in
	neither the late tickets nor the Late % (D-297)."""
	wait = None if t.floor_started else minutes(t.sent_at, t.started_at)
	cook = None if t.floor_started or t.floor_ready else minutes(t.started_at, t.ready_at)
	total = None if t.floor_ready else minutes(t.sent_at, t.ready_at)
	pickup = None if t.floor_ready or t.floor_served else minutes(t.ready_at, t.served_at)
	is_late = None if t.floor_ready else (total if total is not None else minutes(t.sent_at, now)) > late
	return wait, cook, total, pickup, is_late


def execute(filters=None):
	filters = prepare_filters(filters)
	group_by = filters.get("group_by") or "Station"
	late = cint(filters.get("late_minutes") or 20)
	cond = "and t.station = %(station)s" if filters.get("station") else ""
	tickets = frappe.db.sql(
		f"""
		select t.name, t.station, t.waiter, t.sent_at, t.started_at, t.ready_at, t.served_at,
			t.floor_started, t.floor_ready, t.floor_served
		from `tabResto Kitchen Ticket` t
		where t.ticket_type = 'Order' and t.status != 'Cancelled'
			and date(t.sent_at) between %(from_date)s and %(to_date)s {cond}
		""",
		filters,
		as_dict=True,
	)
	items = {}
	if group_by == "Item" and tickets:
		for row in frappe.db.sql(
			"""select parent, item_code, item_name, qty from `tabResto Kitchen Ticket Item`
			where parent in %(names)s""",
			{"names": [t.name for t in tickets]},
			as_dict=True,
		):
			items.setdefault(row.parent, []).append(row)

	now = now_datetime()
	groups = {}
	for t in tickets:
		wait, cook, total, pickup, is_late = spans(t, now, late)
		if group_by == "Item":
			keys = [(r.item_code, r.item_name, flt(r.qty)) for r in items.get(t.name, [])]
		else:
			key = {
				"Station": t.station,
				"Day": str(t.sent_at.date()) if t.sent_at else "",
				"Hour": f"{t.sent_at.hour:02d}:00" if t.sent_at else "",
				"Waiter": frappe.utils.get_fullname(t.waiter) if t.waiter else "",
			}[group_by]
			keys = [(key, "", 1)]
		for key, name, qty in keys:
			g = groups.setdefault(
				key,
				{"group": key, "item_name": name, "tickets": 0, "qty": 0, "late": 0, "timed": 0, "wait": [], "cook": [], "total": [], "pickup": []},
			)
			g["tickets"] += 1
			g["qty"] += qty
			g["late"] += 1 if is_late else 0
			g["timed"] += 0 if is_late is None else 1
			for field, value in (("wait", wait), ("cook", cook), ("total", total), ("pickup", pickup)):
				if value is not None:
					g[field].append(value)

	data = []
	for g in groups.values():
		row = {k: g[k] for k in ("group", "item_name", "tickets", "qty", "late")}
		for field in ("wait", "cook", "total", "pickup"):
			values = g[field]
			row[f"avg_{field}"] = sum(values) / len(values) if values else None
		row["max_total"] = max(g["total"]) if g["total"] else None
		row["late_pct"] = g["late"] / g["timed"] * 100 if g["timed"] else None
		data.append(row)
	if group_by in ("Day", "Hour"):
		data.sort(key=lambda r: r["group"])
	else:
		data.sort(key=lambda r: -(r["avg_total"] or 0))

	columns = [{"fieldname": "group", "label": _(group_by, context="Resto"), "fieldtype": "Data", "width": 150}]
	if group_by == "Item":
		columns.append({"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170})
	columns += [
		{"fieldname": "tickets", "label": _("Tickets"), "fieldtype": "Int", "width": 80},
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 70},
		{"fieldname": "avg_wait", "label": _("Wait to Start (Min)"), "fieldtype": "Float", "precision": 1, "width": 120},
		{"fieldname": "avg_cook", "label": _("Cooking (Min)"), "fieldtype": "Float", "precision": 1, "width": 110},
		{"fieldname": "avg_total", "label": _("Sent to Ready (Min)"), "fieldtype": "Float", "precision": 1, "width": 130},
		{"fieldname": "max_total", "label": _("Longest (Min)"), "fieldtype": "Float", "precision": 1, "width": 110},
		{"fieldname": "avg_pickup", "label": _("Ready to Served (Min)"), "fieldtype": "Float", "precision": 1, "width": 130},
		{"fieldname": "late", "label": _("Late"), "fieldtype": "Int", "width": 70},
		{"fieldname": "late_pct", "label": _("Late %"), "fieldtype": "Percent", "width": 80},
	]
	chart = {
		"data": {
			"labels": [str(r["group"]) for r in data][:30],
			"datasets": [{"name": _("Sent to Ready (Min)"), "values": [flt(r["avg_total"], 1) for r in data][:30]}],
		},
		"type": "bar",
	}
	return columns, data, None, chart


def minutes(start, end):
	if not start or not end:
		return None
	return max(time_diff_in_seconds(end, start), 0) / 60
