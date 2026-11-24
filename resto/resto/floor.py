"""Server API for the floor screen (waiters, cashiers, hosts). Every call checks roles."""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

from resto.resto import billing, kitchen, printing, shift, sides
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
		frappe.throw(_("Order {0} is {1}").format(order.name, _(order.status, context="Resto")))
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

	alerts = kitchen.print_alerts()
	return {
		"halls": halls,
		"orders": [o for o in orders if o.order_type != "Dine In" or o.rebill],
		# Kitchen printing that stopped, shown on the floor too (the kitchen may not look at a screen).
		"kitchen_print": {"unprinted": len(alerts["unprinted"]), "no_screen": alerts["no_screen"]},
		"receipt_mode": printing.receipt_mode(),
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
		"shift": shift.status() if has_role(CASHIER) else None,
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
	# Which unsent lines can have their sides changed (Restaurant Settings and the dish's recipe).
	data["sides_enabled"] = int(sides.enabled())
	for d in data.get("items") or []:
		d["has_sides"] = int(bool(data["sides_enabled"] and d.get("kitchen_status") == "Not Sent" and sides.slots(d.get("item_code"))))
		d["extras_text"] = sides.extras_text(d)
	return data


_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _phone(value):
	"""A phone as one form: Arabic-Indic digits as 0-9, no spaces, dashes or brackets, and an
	Egyptian +20 / 0020 prefix as the leading 0 people dial."""
	text = (value or "").translate(_DIGITS)
	digits = "".join(ch for ch in text if ch.isdigit())
	if not digits:
		return None
	for prefix in ("0020", "20"):
		if (text.strip().startswith("+") or prefix == "0020") and digits.startswith(prefix) and len(digits) == len(prefix) + 10:
			return "0" + digits[len(prefix) :]
	return digits


def _looks_like_phone(text):
	return bool(text) and all(ch.isdigit() or ch in " -+()" for ch in text.translate(_DIGITS))


@frappe.whitelist()
def search_customers(txt=""):
	"""Customers by phone (theirs or a linked contact's: 4 digits or more), or by name or delivery
	address; the latest first."""
	require_role(*STAFF)
	txt = (txt or "").strip()
	if _looks_like_phone(txt):
		phone = _phone(txt) or ""
		if len(phone) < 4:
			return []
		where = """c.mobile_no like %(t)s or c.name in (
			select dl.link_name from `tabDynamic Link` dl
			join `tabContact Phone` cp on cp.parent = dl.parent and cp.parenttype = 'Contact'
			where dl.parenttype = 'Contact' and dl.link_doctype = 'Customer' and cp.phone like %(t)s)"""
		like = f"%{phone}%"
	else:
		if len(txt) < 2:
			return []
		where = "c.name like %(t)s or c.customer_name like %(t)s or c.resto_address like %(t)s"
		like = f"%{txt}%"
	return frappe.db.sql(
		f"""select c.name, c.customer_name, c.mobile_no, c.resto_address
		from `tabCustomer` c
		where c.disabled = 0 and ({where})
		order by c.modified desc
		limit 8""",
		{"t": like},
		as_dict=True,
	)


def _customer_by_phone(phone):
	"""The customer with this phone (theirs or a linked contact's), if any."""
	if not phone:
		return None
	found = frappe.db.get_value("Customer", {"mobile_no": phone, "disabled": 0}, "name")
	if found:
		return found
	rows = frappe.db.sql(
		"""select dl.link_name from `tabDynamic Link` dl
		join `tabContact Phone` cp on cp.parent = dl.parent and cp.parenttype = 'Contact'
		join `tabCustomer` c on c.name = dl.link_name and c.disabled = 0
		where dl.parenttype = 'Contact' and dl.link_doctype = 'Customer' and cp.phone = %s limit 1""",
		phone,
	)
	return rows[0][0] if rows else None


def _leaf(doctype, value):
	"""value when it is a leaf of the tree, else the first leaf (a customer needs one)."""
	if value and not frappe.db.get_value(doctype, value, "is_group"):
		return value
	return frappe.db.get_value(doctype, {"is_group": 0}, "name", order_by="lft")


def _new_customer(name, phone, address=None):
	"""A Customer made at the counter (cashier), always with a phone (one customer per phone): the
	phone goes on its primary contact (ERPNext makes it), the delivery address is kept as typed."""
	require_role(CASHIER)
	name = (name or "").strip()
	if not name:
		frappe.throw(_("Enter the customer's name"))
	selling = frappe.get_cached_doc("Selling Settings")
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Individual",
			"customer_group": _leaf("Customer Group", selling.customer_group),
			"territory": _leaf("Territory", selling.territory),
			"mobile_no": phone,
			"resto_address": (address or "").strip() or None,
		}
	)
	doc.flags.ignore_permissions = True  # the contact too (ERPNext passes the flag on)
	doc.insert()
	return doc.name


def _customer_details(customer, customer_name, phone, address, new_customer, order_type):
	"""(customer, name, phone, address) of an order.

	A chosen customer fills what was left empty (the address on a delivery only); a phone that is
	another customer's is refused. Without a chosen customer a known phone links its customer, and a
	new customer is saved only with a phone (a name alone stays on the order)."""
	phone = _phone(phone)
	delivery = order_type == "Delivery"
	if customer:
		c = frappe.db.get_value("Customer", customer, ["customer_name", "mobile_no", "resto_address", "disabled"], as_dict=True)
		if not c or c.disabled:
			frappe.throw(_("Customer {0} not found").format(customer))
		owner = _customer_by_phone(phone) if phone else None
		if owner and owner != customer:
			frappe.throw(
				_("Phone {0} is the phone of customer {1}; choose that customer or correct the phone").format(
					phone, frappe.bold(frappe.db.get_value("Customer", owner, "customer_name"))
				)
			)
		customer_name = customer_name or c.customer_name
		phone = phone or _phone(c.mobile_no)
		if delivery and address and not c.resto_address and owner == customer and has_role(CASHIER):
			# The first delivery address typed for this phone is kept for next time.
			frappe.db.set_value("Customer", customer, "resto_address", address.strip(), update_modified=False)
		if delivery:
			address = address or c.resto_address
		return customer, customer_name, phone, address
	if phone:
		known = _customer_by_phone(phone)
		if known:
			c = frappe.db.get_value("Customer", known, ["customer_name", "resto_address"], as_dict=True)
			frappe.msgprint(
				_("Phone {0} is customer {1}'s: the order is theirs").format(phone, frappe.bold(c.customer_name)),
				alert=True,
				indicator="blue",
			)
			return known, customer_name or c.customer_name, phone, (address or c.resto_address) if delivery else address
		if cint(new_customer) and (customer_name or "").strip():
			return _new_customer(customer_name, phone, address if delivery else None), customer_name.strip(), phone, address
	return None, customer_name, phone, address


# --------------------------------------------------------------------------- orders


@frappe.whitelist(methods=["POST"])
def create_order(
	order_type="Dine In", table=None, guests=1, customer=None, customer_name=None, phone=None, address=None, new_customer=0
):
	require_role(WAITER, CASHIER)
	customer, customer_name, phone, address = _customer_details(
		customer, customer_name, phone, address, new_customer, order_type
	)
	order = frappe.new_doc("Resto Order")
	order.update(
		{
			"order_type": order_type,
			"resto_table": table if order_type == "Dine In" else None,
			"guests": cint(guests) or 1,
			"customer": customer,
			"customer_name": customer_name,
			"phone": phone,
			"address": address,
			"delivery_status": "Pending" if order_type == "Delivery" else None,
		}
	)
	order.flags.ignore_permissions = True
	order.insert()
	return order.name


@frappe.whitelist(methods=["POST"])
def update_order_info(
	order, guests=None, customer=None, customer_name=None, phone=None, address=None, notes=None, new_customer=0
):
	require_role(WAITER, CASHIER, HOST)
	doc = _get_order(order)
	if customer or phone or cint(new_customer):
		customer, customer_name, phone, address = _customer_details(
			customer, customer_name, phone, address, new_customer, doc.order_type
		)
		customer = customer or ""  # none found: the order's link is cleared, as asked
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


def _plain_unsent_line(doc, it):
	"""An unsent line of the same dish with nothing of its own (modifiers, note, course, hold, changed
	sides or extras), when what is added has nothing either: its quantity goes up instead."""
	if it.get("modifiers") or (it.get("notes") or "").strip() or it.get("course") or cint(it.get("hold")):
		return None
	for d in doc.items:
		if (
			d.item_code == it.get("item_code")
			and d.kitchen_status == "Not Sent"
			and not json.loads(d.modifiers or "[]")
			and not (d.notes or "").strip()
			and not d.course
			and not cint(d.hold)
			and not d.sides
			and not d.get("sides_extras")
			and not flt(d.billed_qty)
		):
			return d
	return None


@frappe.whitelist(methods=["POST"])
def add_items(order, items):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	for it in _loads(items, []):
		qty = flt(it.get("qty") or 1)
		same = _plain_unsent_line(doc, it)
		if same:
			# The same dish tapped again: one line with more dishes, not a second line.
			same.qty = flt(same.qty) + qty
			continue
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


@frappe.whitelist()
def get_sides(order, lines, together=0):
	"""Sides of unsent lines: per dish for one line, in total for several lines (or for one line
	chosen as "several dishes": the total for its whole quantity)."""
	require_role(WAITER, CASHIER)
	if not sides.enabled():
		frappe.throw(_("Changing the sides served is off in Restaurant Settings"))
	doc = _get_order(order)
	lines = _loads(lines, [])
	# Extra portions above the recipe's sides are charged: the screen shows their price.
	charge = sides.charge_extras()
	if len(lines) == 1 and not cint(together):
		(row,) = sides._unsent_rows(doc, lines)
		choice = sides.line_choice(row)
		result = []
		for key, slot in sides.slots(row.item_code).items():
			chosen = choice.get(key, {}).get("items") or {}
			result.append(
				{
					"key": key,
					"label": slot.label,
					"max": slot.max,
					"free": sum(slot.default.values()) if charge and slot.price else 0,
					"single": int(not slot.group and slot.max == 1),
					"shared": bool(choice.get(key, {}).get("set")),
					"options": [
						{
							"item": i,
							"item_name": slot.names.get(i, i),
							"total": flt(chosen.get(i), 6),
							"price": slot.price.get(i, 0) if charge else 0,
							"default": int(i in slot.default),
						}
						for i in slot.portion
					],
				}
			)
		return {"per_dish": 1, "qty": flt(row.qty), "title": f"{row.item_name} × {sides._num(row.qty)}", "slots": result}
	rows = sides._unsent_rows(doc, lines)
	slots_ = sides.combined(doc, lines)
	if charge:
		for c in slots_:
			having = [d for d in rows if c["key"] in sides.slots(d.item_code)]
			prices, defaults = {}, set()
			for d in having:
				for i, p in sides.slots(d.item_code)[c["key"]].price.items():
					prices.setdefault(i, p)
				defaults.update(sides.slots(d.item_code)[c["key"]].default)
			c["free"] = sum(sum(sides.slots(d.item_code)[c["key"]].default.values()) * flt(d.qty) for d in having) if prices else 0
			for o in c["options"]:
				o["price"] = prices.get(o["item"], 0)
				o["default"] = int(o["item"] in defaults)
	return {"per_dish": 0, "title": "، ".join(f"{d.item_name} × {sides._num(d.qty)}" for d in rows), "slots": slots_}


@frappe.whitelist(methods=["POST"])
def set_sides(order, lines, sides_choice, together=0):
	"""sides_choice: {slot key: {item: portions}} of the slots changed, per dish for one line, in
	total for several lines (or together)."""
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	lines = _loads(lines, [])
	choice = _loads(sides_choice, {})
	if not isinstance(lines, list) or not isinstance(choice, dict):
		frappe.throw(_("The sides could not be read"))
	if not choice:
		return doc.name
	if len(lines) == 1 and not cint(together):
		sides.set_for_line(doc, lines[0], choice)
	else:
		sides.set_for_lines(doc, lines, choice)
	_reopen_if_billed(doc)
	return _save(doc).name


@frappe.whitelist(methods=["POST"])
def set_print_sides(order, print_sides):
	require_role(WAITER, CASHIER)
	doc = _get_order(order)
	doc.print_sides = cint(print_sides)
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
	if cint(cooked) and was_sent:
		# Its share of a shared side is booked as waste with it: the other dishes keep theirs.
		doc.flags.resto_keep_sets = {e["set"] for e in json.loads(row.sides or "{}").values() if e.get("set")}
	kitchen.void_line(doc, row, reason.strip())
	_reopen_if_billed(doc)
	_save(doc)
	doc.add_comment("Info", _("Voided {0}: {1}").format(row.item_name, reason))
	if cint(cooked) and was_sent:
		_waste_for_void(doc, row, reason.strip())
	return doc.name


def _deducts_stock(item_code):
	"""A stock item, or a dish whose bundle deducts stock (a priced modifier may be neither)."""
	if frappe.get_cached_value("Item", item_code, "is_stock_item"):
		return True
	return bool(frappe.db.get_value("Product Bundle", {"new_item_code": item_code, "disabled": 0}, "name"))


# A voided line as the waste reads it: fresh from the database (the marks of a waste posted a moment ago).
_LINE_FIELDS = ["name", "item_code", "item_name", "qty", "modifiers", "sides", "sides_qty", "sides_extras", "kitchen_status", "void_waste"]


def _order_lines(order):
	return frappe.get_all("Resto Order Item", filters={"parent": order.name, "parenttype": "Resto Order"}, fields=_LINE_FIELDS)


def _slot_part(row, key):
	"""({side item: stock qty} free, {billing item: portions} extra) per dish of one shared choice of a
	line, as its bill deducts them (extra portions of a set are billed apart, as their own item)."""
	entry = (sides._loads(row.get("sides")) or {}).get(key) or {}
	extras = [e for e in (sides._loads(row.get("sides_extras")) or {}).values() if e.get("set") and e.get("set") == entry.get("set")]
	billed = {}
	for option, e in (sides._loads(row.get("sides_extras")) or {}).items():
		if e.get("set") and e.get("set") == entry.get("set"):
			billed[option] = flt(e.get("portions"))
	items = {i: flt(p) - flt(billed.get(i)) for i, p in (entry.get("items") or {}).items()}
	free = sides.qty_per_dish(row.item_code, {key: {**entry, "items": {i: p for i, p in items.items() if p > 1e-9}}})
	extra = {}
	for e in extras:
		if flt(e.get("portions")) > 0 and e.get("item"):
			extra[e["item"]] = extra.get(e["item"], 0) + flt(e["portions"])
	return free, extra


def _mark_share(line, key, flag, value=None):
	"""Set (value) or clear (None) a mark on one shared choice of a line, in the database only."""
	choice = sides._loads(frappe.db.get_value("Resto Order Item", line, "sides", for_update=True)) or {}
	entry = choice.get(key)
	if not isinstance(entry, dict):
		return
	if value is None:
		entry.pop(flag, None)
	else:
		entry[flag] = value
	frappe.db.set_value("Resto Order Item", line, "sides", sides._dumps(choice), update_modified=False)


def _waste_items(order, row, qty):
	"""({item: stock qty}, [(line, slot)]): what the kitchen made for qty dishes of a voided line, as
	billing would deduct it, and the shares of a shared side it takes from other lines.

	A shared side goes with its dish: it is deducted when the dish is billed or wasted, or it went to
	the other dishes of the set when the dish was voided uncooked (they deduct it now: "moved"). When
	an order is cancelled the kitchen had plated each set's total at once ("k"): the first waste of a
	dish of the set takes the shares of all its dishes, every dish of its own line too. Every share a
	waste holds is marked with it ("w"), so no share is wasted twice and cancelling it frees them."""
	out = {}

	def add(item, q):
		if q > 1e-9:
			out[item] = out.get(item, 0) + q

	taken = []
	choice = sides._loads(row.get("sides")) or {}
	if choice:
		# What one dish served, as frozen when it went to the kitchen; its shared choices apart.
		frozen = sides._loads(row.get("sides_qty"))  # "{}": every side of the dish is a billed extra
		free = dict(frozen if frozen is not None else sides.qty_per_dish(row.item_code, choice))
		extra = {}
		for e in (sides._loads(row.get("sides_extras")) or {}).values():
			if flt(e.get("portions")) > 0 and e.get("item"):
				extra[e["item"]] = extra.get(e["item"], 0) + flt(e["portions"])
		together = set()
		for key, set_id in sides.shared_slots(row).items():
			entry = choice[key]
			slot_free, slot_extra = _slot_part(row, key)
			for item, q in slot_free.items():
				free[item] = max(flt(free.get(item)) - q, 0)
			for item, q in slot_extra.items():
				extra[item] = max(flt(extra.get(item)) - q, 0)
			if entry.get("moved") or entry.get("w"):
				continue
			dishes = flt(row.qty) if entry.get("k") else qty
			for item, q in sides._flat(slot_free).items():
				add(item, q * dishes)
			for item, q in slot_extra.items():
				add(item, q * dishes)
			# Marked as in this waste too: cancelling it frees the share, and only it.
			taken.append((row.name, key))
			if entry.get("k"):
				together.add((set_id, key))
		for item, q in sides.dish_components(row.item_code, {k: v for k, v in free.items() if v > 1e-9}).items():
			add(item, q * qty)
		for item, q in extra.items():
			add(item, q * qty)
		for d in _order_lines(order) if together else ():
			# "w" says whose waste a share is in (a dish wasted after its share was taken has none).
			if d.name == row.name or d.kitchen_status != "Void":
				continue
			for key, set_id in sides.shared_slots(d).items():
				entry = sides._loads(d.sides)[key]
				if (set_id, key) not in together or not entry.get("k") or entry.get("moved") or entry.get("w"):
					continue
				slot_free, slot_extra = _slot_part(d, key)
				for item, q in sides._flat(slot_free).items():
					add(item, q * flt(d.qty))
				for item, q in slot_extra.items():
					add(item, q * flt(d.qty))
				taken.append((d.name, key))
	else:
		add(row.item_code, qty)
	for mod in json.loads(row.modifiers or "[]"):
		if flt(mod.get("price")) and mod.get("item") and _deducts_stock(mod["item"]):
			add(mod["item"], qty)
	return out, taken


def _waste_for_void(order, row, reason, qty=None):
	"""Post the waste of a dish voided after cooking (qty dishes of the line, all by default) and link
	it on the line. Returns the Resto Waste, or None when it could not be posted (the void stands)."""
	# A locking read: a second request at the same moment sees the waste the first one posted.
	line = frappe.db.get_value("Resto Order Item", row.name, _LINE_FIELDS, as_dict=True, for_update=True)
	if line.void_waste:
		frappe.msgprint(
			_("Waste {0} is already posted for {1}").format(line.void_waste, frappe.bold(line.item_name)), indicator="orange"
		)
		return None
	qty = flt(line.qty) if qty is None else flt(qty)
	messages = len(frappe.local.message_log or [])
	frappe.db.savepoint("resto_void_waste")
	try:
		items, taken = _waste_items(order, line, qty)
		waste = frappe.get_doc(
			{
				"doctype": "Resto Waste",
				"remarks": _("Voided after cooking on order {0}: {1}").format(order.name, reason),
				"resto_order": order.name,
				"order_item": line.name,
				"items": [{"item_code": k, "qty": v, "reason": "Other", "note": reason} for k, v in items.items()],
			}
		)
		waste.flags.ignore_permissions = True
		waste.insert()
		waste.submit()
		frappe.db.set_value("Resto Order Item", line.name, "void_waste", waste.name, update_modified=False)
		for name, key in taken:
			_mark_share(name, key, "w", waste.name)
		# A form of the order opened before this cannot be saved over the link.
		frappe.db.set_value("Resto Order", order.name, "modified", now_datetime(), update_modified=False)
		order.add_comment("Info", _("Waste {0} posted for the voided {1}").format(waste.name, line.item_name))
		return waste.name
	except Exception:
		frappe.db.rollback(save_point="resto_void_waste")
		# Only this attempt's messages go; a warning about another dish stays.
		if frappe.local.message_log:
			del frappe.local.message_log[messages:]
		frappe.log_error(title=f"Resto void waste for {order.name}")
		frappe.msgprint(
			_("{0} is void, but its waste could not be posted. Try again with Post Waste for Cooked Dishes on the order, or tell the manager.").format(
				frappe.bold(line.item_name)
			),
			indicator="orange",
		)
		return None


def _cooked_qty(value, rows):
	"""{line: dishes cooked} from a list of lines (all their dishes) or {line: qty}."""
	value = _loads(value, []) if value else []
	if isinstance(value, list):
		value = {name: None for name in value}
	if not isinstance(value, dict):
		frappe.throw(_("The cooked dishes could not be read"))
	by_name = {d.name: d for d in rows}
	out = {}
	for name, qty in value.items():
		row = by_name.get(name)
		if not row:
			frappe.throw(_("Only items sent to the kitchen can be marked as cooked"))
		qty = flt(row.qty) if qty is None else flt(qty)
		if qty < 0 or qty > flt(row.qty) + 1e-9:
			frappe.throw(_("{0}: cooked quantity must be between 0 and {1}").format(frappe.bold(row.item_name), flt(row.qty)))
		if qty > 1e-9:
			out[name] = qty
	return out


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
	if cint(src.print_sides):
		tgt.print_sides = 1
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


# The cashier or the kitchen may post waste for a dish voided after sending (a cancelled order too) this
# long after its void ticket; a manager any time.
COOKED_WASTE_HOURS = 12


@frappe.whitelist(methods=["POST"])
def cancel_order(order, reason, cooked=None):
	"""Cancel an order. With sent items this needs a manager and the kitchen gets void tickets.

	cooked: the sent lines the kitchen already made (JSON list of line names, or {line: dishes}): their
	ingredients are posted as waste, as for a void after cooking; the others stay in stock."""
	require_role(WAITER, CASHIER)
	if not (reason or "").strip():
		frappe.throw(_("Enter a reason"))
	reason = reason.strip()
	doc = _get_order(order)
	if any(not d.cancelled for d in doc.invoices):
		frappe.throw(_("Part of this order is paid; it cannot be cancelled"))
	sent = [d for d in doc.items if d.kitchen_status not in ("Not Sent", "Void")]
	if sent:
		require_role(MANAGER)
	cooked = _cooked_qty(cooked, sent)
	# The lines whose shared sides the kitchen plated together (a held line's share went on the ticket).
	together = [d for d in doc.items if d.kitchen_status != "Void" and sides.shared_slots(d)]
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
	if sent:
		for d in together:
			for key in sides.shared_slots(d):
				_mark_share(d.name, key, "k", 1)
	for d in sent:
		if d.name in cooked:
			_waste_for_void(doc, d, reason, cooked[d.name])
	return doc.name


@frappe.whitelist(methods=["POST"])
def post_cooked_waste(order, lines):
	"""Waste for dishes the kitchen made that were voided after sending without it (the order cancelled
	without ticking them, or a void answered "not cooked"), from the order (cashier, manager) or the void
	ticket on the kitchen screen. Each line once. The cashier and the kitchen: within COOKED_WASTE_HOURS
	of its void ticket; a manager any time."""
	require_role(CASHIER, KITCHEN)
	doc = frappe.get_doc("Resto Order", order, for_update=True)
	asked = _loads(lines, []) if lines else []
	names = set(asked if isinstance(asked, list) else asked if isinstance(asked, dict) else [])
	if not names:
		frappe.throw(_("Choose the dishes that were cooked"))
	manager = has_role()
	rows = [d for d in doc.items if d.name in names]
	if len(rows) != len(names):
		frappe.throw(_("Some of these dishes are not on order {0}").format(doc.name))
	cooked = _cooked_qty(asked, rows)
	since = add_to_date(now_datetime(), hours=-COOKED_WASTE_HOURS)
	for d in rows:
		if not (d.ticket and d.kitchen_status == "Void"):
			frappe.throw(_("{0} was not voided after it was sent to the kitchen").format(frappe.bold(d.item_name)))
		# A locking read: a second press at the same moment sees the waste the first one posted.
		posted = frappe.db.get_value("Resto Order Item", d.name, "void_waste", for_update=True)
		if posted:
			frappe.throw(_("Waste {0} is already posted for {1}").format(posted, frappe.bold(d.item_name)))
		if not manager:
			voided_at = kitchen.void_sent_at(d.name)
			if not voided_at or get_datetime(voided_at) < since:
				frappe.throw(
					_("{0} was voided more than {1} hours ago: ask a manager").format(frappe.bold(d.item_name), COOKED_WASTE_HOURS)
				)
	posted = {}
	for d in rows:
		if d.name in cooked:
			posted[d.name] = _waste_for_void(doc, d, d.void_reason or doc.cancel_reason or "", cooked[d.name])
	publish("order", order=doc.name)
	publish("ticket", order=doc.name)
	return posted


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
	# The bill prints on the cashier printer, or the screen that asked for it prints its pages
	# (all its copies, each with its label), or it is not printed (Resto Settings).
	mode = printing.bill_mode()
	preview["on_printer"] = printing.queue_bill(doc.name) if mode == "server" else False
	preview["pages"] = []
	if mode == "browser":
		messages = len(frappe.local.message_log or [])
		try:
			preview["pages"] = printing.bill_pages(doc.name)
		except Exception:
			# The waiter sees the short red notice, not the template error.
			if frappe.local.message_log:
				del frappe.local.message_log[messages:]
			# A broken bill format must not stop the bill: it is requested, and printed once fixed.
			frappe.log_error(title=_("Bill {0} was not printed").format(doc.name))
			preview["print_error"] = 1
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
		"receipt_on_printer": printing.receipt_on_printer(),
		# server / auto / button / none (printing.receipt_mode); auto prints it unless printed before
		# (a retried payment returns the invoice already paid).
		"receipt_mode": printing.receipt_mode(),
		# Read from the document: a payment never fails on a custom column a broken migrate left out.
		"receipt_printed": cint(si.get("resto_receipt_printed")),
	}


# --------------------------------------------------------------------------- reservations


@frappe.whitelist(methods=["POST"])
def seat_reservation(reservation, table=None):
	require_role(HOST, WAITER)
	res = frappe.get_doc("Resto Reservation", reservation, for_update=True)
	if res.status != "Booked":
		frappe.throw(_("Reservation {0} is {1}").format(res.name, _(res.status, context="Resto")))
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
