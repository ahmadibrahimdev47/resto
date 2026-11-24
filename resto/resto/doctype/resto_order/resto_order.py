import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, now_datetime

from resto.resto import sides
from resto.resto.utils import CASHIER, MANAGER, WAITER, get_settings, has_role

ACTIVE = ("Open", "Bill Requested")
CLOSED = ("Paid", "Cancelled", "Merged")
NOT_SENT = "Not Sent"
VOID = "Void"
# Fields of a line that cannot change once it went to the kitchen (void and re-add instead).
LOCKED_LINE_FIELDS = ("item_code", "qty", "rate", "modifiers_amount", "modifiers", "sides", "sides_amount")
SERVER_HEADER_FIELDS = (
	"status",
	"opened_at",
	"closed_at",
	"bill_service_amount",
	"bill_tax_amount",
	"bill_grand_total",
	"bill_requested_at",
	"bill_details",
	"merged_into",
	"cancel_reason",
	"reservation",
	"company",
	"pos_profile",
	"rebill",
)
# Changes that make a printed bill wrong.
BILL_FIELDS = ("discount_amount", "service_charge_percent", "delivery_fee", "customer", "net_total")
BILL_LINE_FIELDS = ("qty", "rate", "modifiers_amount", "sides_amount", "kitchen_status", "billed_qty")
SERVER_LINE_FIELDS = ("kitchen_status", "station", "ticket", "sent_at", "billed_qty", "void_reason", "voided_by", "sides", "sides_text", "sides_qty", "sides_extras", "sides_amount", "void_waste")


class RestoOrder(Document):
	def before_insert(self):
		self.status = "Open"
		self.opened_at = now_datetime()
		settings = get_settings()
		if not self.flags.resto_api:
			# A new order made outside Resto's API starts clean: no bill, no invoices, the
			# settings' company, POS Profile and service charge.
			for field in SERVER_HEADER_FIELDS:
				if field not in ("status", "opened_at"):
					self.set(field, None)
			self.set("invoices", [])
			if not has_role():
				self.service_charge_percent = None
		if not self.company:
			self.company = settings.company
		if not self.pos_profile:
			self.pos_profile = settings.pos_profile
		if not self.service_charge_percent:
			self.service_charge_percent = default_service_percent(settings, self.order_type)
		if self.order_type == "Delivery" and not self.delivery_fee:
			self.delivery_fee = flt(settings.delivery_fee)
		if not self.waiter:
			self.waiter = frappe.session.user

	def validate(self):
		self.before_doc = self.get_doc_before_save() if not self.is_new() else None
		self.validate_closed()
		if self.before_doc and not self.flags.resto_api:
			for field in SERVER_HEADER_FIELDS:
				self.set(field, self.before_doc.get(field))
			# Invoices are written only by billing.
			self.set("invoices", [d.as_dict() for d in self.before_doc.invoices])
		self.set_price_list()
		self.validate_order_type()
		self.validate_table()
		self.validate_lines()
		self.calculate_totals()
		self.validate_discount()
		if (
			self.before_doc
			and flt(self.before_doc.service_charge_percent) != flt(self.service_charge_percent)
			and not has_role()
		):
			frappe.throw(_("Only a Resto Manager can change the service charge"), frappe.PermissionError)
		if (
			self.before_doc
			and flt(self.before_doc.delivery_fee) != flt(self.delivery_fee)
			and not has_role(CASHIER)
		):
			frappe.throw(_("Only a cashier can change the delivery fee"), frappe.PermissionError)
		self.reopen_changed_bill()
		self.close_when_all_billed()
		self.set_title()

	def reopen_changed_bill(self):
		"""A printed bill is only valid while nothing on it changes: any change sends the order back
		to Open, whatever screen or API made the change (a partial payment included)."""
		before = self.before_doc
		if not before or before.status != "Bill Requested" or self.status != "Bill Requested":
			return
		changed = any(flt(before.get(f)) != flt(self.get(f)) if f != "customer" else before.get(f) != self.get(f) for f in BILL_FIELDS)
		old_lines = {d.name: tuple(_norm(d.get(f)) for f in BILL_LINE_FIELDS) for d in before.items}
		new_lines = {d.name: tuple(_norm(d.get(f)) for f in BILL_LINE_FIELDS) for d in self.items}
		if changed or old_lines != new_lines:
			self.status = "Open"
			self.bill_requested_at = None
			self.bill_details = None
			self.bill_service_amount = self.bill_tax_amount = self.bill_grand_total = 0

	def close_kitchen_tickets(self):
		"""Paid: food waiting at the pass was served. Cancelled: nothing is left to cook."""
		if self.status == "Paid":
			frappe.db.sql(
				"""update `tabResto Kitchen Ticket` set status = 'Served', served_at = %s
				where resto_order = %s and status = 'Ready'""",
				(now_datetime(), self.name),
			)
		elif self.status == "Cancelled":
			frappe.db.sql(
				"""update `tabResto Kitchen Ticket` set status = 'Cancelled'
				where resto_order = %s and ticket_type = 'Order' and status in ('New', 'In Progress', 'Ready')""",
				self.name,
			)

	def close_when_all_billed(self):
		"""Part of the order was paid and the rest was voided or removed: nothing is left to bill."""
		if self.status in ACTIVE and any(not d.cancelled for d in self.invoices) and not self.unbilled_lines():
			self.status = "Paid"
			self.closed_at = self.closed_at or now_datetime()
			self.rebill = 0

	# ------------------------------------------------------------------ checks

	def validate_closed(self):
		if self.before_doc and self.before_doc.status in CLOSED and not self.flags.resto_allow_closed:
			frappe.throw(_("Order {0} is {1} and cannot be changed").format(self.name, _(self.before_doc.status, context="Resto")))

	def set_price_list(self):
		if self.pos_profile:
			profile_company, price_list = frappe.db.get_value(
				"POS Profile", self.pos_profile, ["company", "selling_price_list"]
			)
			if profile_company != self.company:
				frappe.throw(
					_("POS Profile {0} does not belong to company {1}").format(
						frappe.bold(self.pos_profile), frappe.bold(self.company)
					)
				)
			self.price_list = get_settings().selling_price_list or price_list
		elif not self.price_list:
			self.price_list = get_settings().selling_price_list
		if not self.price_list:
			frappe.throw(_("Set a POS Profile or a Selling Price List in Resto Settings first"))

	def validate_order_type(self):
		if self.order_type == "Dine In":
			if not self.resto_table:
				frappe.throw(_("Choose a table for a dine-in order"))
			if cint(self.guests) < 1:
				self.guests = 1
		else:
			self.resto_table = None
			self.hall = None
		if self.order_type != "Delivery":
			self.delivery_fee = 0
		if flt(self.delivery_fee) < 0:
			frappe.throw(_("Delivery fee cannot be negative"))
		if self.order_type == "Delivery" and flt(self.delivery_fee) and not get_settings().delivery_fee_item:
			frappe.throw(_("Set the Delivery Fee Item in Resto Settings first"))

	def validate_table(self):
		if not self.resto_table or self.status not in ACTIVE or self.flags.resto_allow_closed or cint(self.rebill):
			# An order re-opened by a cancelled invoice (rebill) keeps its table even if the table
			# has a new guest by now; it is listed apart on the floor.
			return
		if not cint(frappe.db.get_value("Resto Table", self.resto_table, "is_active")):
			frappe.throw(_("Table {0} is not active").format(frappe.bold(self.resto_table)))
		# Two waiters opening the same table at once: the second one waits here, then sees the first.
		frappe.db.sql("select name from `tabResto Table` where name = %s for update", self.resto_table)
		other = frappe.db.get_value(
			"Resto Order",
			{"resto_table": self.resto_table, "status": ("in", ACTIVE), "name": ("!=", self.name), "rebill": 0},
			"name",
			for_update=True,  # a locking read sees the order the other waiter just committed
		)
		if other:
			frappe.throw(
				_("Table {0} already has the open order {1}").format(frappe.bold(self.resto_table), other)
			)

	def validate_lines(self):
		sides.clear_cache()
		old_rows = {d.name: d for d in self.before_doc.items} if self.before_doc else {}
		current = {d.name for d in self.items if d.name}

		for name, old in old_rows.items():
			if old.kitchen_status != NOT_SENT and name not in current:
				frappe.throw(
					_("Item {0} was sent to the kitchen and cannot be removed; void it instead").format(
						frappe.bold(old.item_name or old.item_code)
					)
				)
			if flt(old.billed_qty) and name not in current:
				frappe.throw(_("Item {0} is already billed").format(frappe.bold(old.item_name)))

		for row in self.items:
			old = old_rows.get(row.name)
			if not self.flags.resto_api:
				# Kitchen and billing state is only changed by Resto's own server code.
				for field in SERVER_LINE_FIELDS:
					row.set(field, old.get(field) if old else None)
				if not old:
					row.kitchen_status = NOT_SENT
					row.billed_qty = 0
			if old and old.kitchen_status != NOT_SENT:
				for field in LOCKED_LINE_FIELDS:
					if _norm(old.get(field)) != _norm(row.get(field)):
						frappe.throw(
							_("Row {0}: {1} was sent to the kitchen; void it and add it again to change it").format(
								row.idx, frappe.bold(row.item_name or row.item_code)
							)
						)
				continue

			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: quantity must be more than zero").format(row.idx))
			if row.kitchen_status == VOID and not old:
				frappe.throw(_("Row {0}: a new line cannot be void").format(row.idx))

			item = frappe.get_cached_value(
				"Item", row.item_code, ["item_name", "disabled", "is_sales_item", "has_variants"], as_dict=True
			)
			if not item or item.disabled or not item.is_sales_item or item.has_variants:
				frappe.throw(_("Row {0}: item {1} cannot be sold").format(row.idx, frappe.bold(row.item_code)))
			row.item_name = item.item_name
			if not old or old.item_code != row.item_code:
				# Prices always come from the price list, never from the screen.
				row.rate = get_item_rate(row.item_code, self.price_list)
			else:
				# Same line: keep the price it got (a form or REST edit cannot change it).
				row.rate = old.rate
			set_line_modifiers(row)
			sides.set_line_sides(row, old)
			if not row.kitchen_status:
				row.kitchen_status = NOT_SENT
		sides.check_shared(self)
		sides.set_order_texts(self)

	def calculate_totals(self):
		total_qty = net_total = 0
		for row in self.items:
			row.amount = flt(flt(row.qty) * (flt(row.rate) + flt(row.modifiers_amount) + flt(row.sides_amount)), row.precision("amount"))
			if row.kitchen_status == VOID:
				continue
			total_qty += flt(row.qty)
			net_total += row.amount
		self.total_qty = total_qty
		self.net_total = flt(net_total, self.precision("net_total"))

		if flt(self.discount_percentage) < 0 or flt(self.discount_percentage) > 100:
			frappe.throw(_("Discount % must be between 0 and 100"))
		if flt(self.discount_percentage):
			self.discount_amount = flt(
				self.net_total * flt(self.discount_percentage) / 100, self.precision("discount_amount")
			)
		if flt(self.discount_amount) < 0:
			frappe.throw(_("Discount amount cannot be negative"))
		if flt(self.discount_amount) > self.net_total:
			frappe.throw(_("Discount cannot be more than the items total"))
		if flt(self.service_charge_percent) < 0:
			frappe.throw(_("Service charge cannot be negative"))

	def validate_discount(self):
		"""Check the discount against the user's limit whenever it grows: a new discount, or the
		same discount on fewer items (removed lines, or a part already paid)."""
		if not flt(self.discount_amount) or self.flags.resto_billing:
			# Settling or cancelling an invoice does not change the discount.
			return
		if not self.discount_reason:
			frappe.throw(_("Enter a reason for the discount"))
		base, discount = discount_base(self)
		if self.before_doc and not self.is_new():
			old_base, old_discount = discount_base(self.before_doc)
			if old_base and discount <= old_discount * base / old_base + 0.01:
				return  # not bigger than before (as a share of what it applies to)
		limit = discount_limit()
		# Compared in money, to the piaster: 10% of 33.25 is 3.33, not "10.02%".
		allowed = flt(base * limit / 100, self.precision("discount_amount"))
		if discount > allowed + 0.005:
			frappe.throw(
				_("A discount of {0}% needs the Resto Manager role (your limit is {1}%)").format(
					flt(discount / base * 100 if base else 100, 2), flt(limit, 2)
				),
				frappe.PermissionError,
			)

	def set_title(self):
		if self.order_type == "Dine In":
			self.title = _("Table {0}", context="Resto").format(self.resto_table)
		else:
			self.title = f"{_(self.order_type, context='Resto')} {self.customer_name or self.phone or ''}".strip()

	# ------------------------------------------------------------------ helpers

	def active_lines(self):
		return [d for d in self.items if d.kitchen_status != VOID]

	def unbilled_lines(self):
		return [d for d in self.active_lines() if flt(d.qty) - flt(d.billed_qty) > 0.0000001]

	def invoiced_total(self):
		return sum(flt(d.grand_total) for d in self.invoices if not d.cancelled)

	def on_update(self):
		from resto.resto.utils import publish

		if self.has_value_changed("status"):
			self.close_kitchen_tickets()
		publish("order", order=self.name, table=self.resto_table, status=self.status)


def _norm(value):
	"""Compare 2 and 2.0 (a form sends numbers as it likes), and None, "" and 0, as equal."""
	if isinstance(value, str):
		try:
			value = float(value)
		except ValueError:
			return value
	if isinstance(value, int | float):
		return flt(value, 9) or ""
	return value or ""


def default_service_percent(settings, order_type):
	field = {
		"Dine In": "service_on_dine_in",
		"Takeaway": "service_on_takeaway",
		"Delivery": "service_on_delivery",
	}.get(order_type)
	return flt(settings.service_charge_percent) if field and cint(settings.get(field)) else 0


def effective_discount_percent(doc):
	if not doc or not flt(doc.net_total):
		return 0
	return flt(doc.discount_amount) / flt(doc.net_total) * 100


def discount_base(doc):
	"""(amount the discount still applies to, discount still to give).

	Once a part of the order is paid, what is left of the discount goes on the unbilled items
	only, so that is what the limit is checked against.
	"""
	active = [d.sales_invoice for d in doc.invoices if not d.cancelled]
	if not active:
		return flt(doc.net_total), flt(doc.discount_amount)
	given = flt(
		frappe.db.sql("select sum(discount_amount) from `tabSales Invoice` where name in %s and docstatus = 1", (active,))[0][0]
	)
	unbilled = sum(
		(flt(d.qty) - flt(d.billed_qty)) * (flt(d.rate) + flt(d.modifiers_amount) + flt(d.sides_amount)) for d in doc.unbilled_lines()
	)
	return unbilled, max(flt(doc.discount_amount) - given, 0)


def discount_limit():
	if has_role():  # Resto Manager or System Manager
		return 100
	settings = get_settings()
	limits = []
	if CASHIER in frappe.get_roles():
		limits.append(flt(settings.cashier_discount_limit))
	if WAITER in frappe.get_roles():
		limits.append(flt(settings.waiter_discount_limit))
	return max(limits) if limits else 0


def get_item_rate(item_code, price_list, throw=True):
	"""Selling rate of an item from the price list today (no customer-specific prices)."""
	today = frappe.utils.getdate()
	rows = frappe.get_all(
		"Item Price",
		filters={"item_code": item_code, "price_list": price_list, "selling": 1},
		fields=["price_list_rate", "customer", "valid_from", "valid_upto", "uom"],
		order_by="valid_from desc, modified desc",
	)
	stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	for row in rows:
		if row.customer:
			continue
		if row.valid_from and get_datetime(row.valid_from).date() > today:
			continue
		if row.valid_upto and get_datetime(row.valid_upto).date() < today:
			continue
		if row.uom and row.uom != stock_uom:
			continue
		return flt(row.price_list_rate)
	if not throw:
		return 0
	frappe.throw(
		_("Item {0} has no price in price list {1}").format(frappe.bold(item_code), frappe.bold(price_list))
	)


def set_line_modifiers(row):
	"""Resolve the chosen modifiers of a line against the modifier groups, server side.

	row.modifiers is a JSON list of {"group": ..., "option": ...}; prices and billing items are
	taken from the group, never from the client.
	"""
	from resto.resto.doctype.resto_modifier_group.resto_modifier_group import groups_for_item

	chosen = row.modifiers
	if isinstance(chosen, str):
		chosen = json.loads(chosen or "[]")
	chosen = chosen or []
	groups = {g["name"]: g for g in groups_for_item(row.item_code)}

	resolved, texts, extra = [], [], 0
	count_by_group = {}
	for choice in chosen:
		group = groups.get(choice.get("group"))
		if not group:
			frappe.throw(
				_("Row {0}: modifier group {1} is not available for {2}").format(
					row.idx, frappe.bold(choice.get("group")), frappe.bold(row.item_code)
				)
			)
		option = next((o for o in group["options"] if o["option_name"] == choice.get("option")), None)
		if not option:
			frappe.throw(
				_("Row {0}: {1} is not an option of {2}").format(
					row.idx, frappe.bold(choice.get("option")), frappe.bold(group["name"])
				)
			)
		count_by_group[group["name"]] = count_by_group.get(group["name"], 0) + 1
		resolved.append(
			{"group": group["name"], "option": option["option_name"], "price": option["price"], "item": option["item"]}
		)
		texts.append(option["option_name"] + (f" (+{frappe.format(option['price'], 'Currency')})" if option["price"] else ""))
		extra += flt(option["price"])

	for group in groups.values():
		n = count_by_group.get(group["name"], 0)
		if group["min_select"] and n < group["min_select"]:
			frappe.throw(
				_("Row {0}: choose at least {1} from {2}").format(row.idx, group["min_select"], frappe.bold(group["name"]))
			)
		if group["max_select"] and n > group["max_select"]:
			frappe.throw(
				_("Row {0}: choose at most {1} from {2}").format(row.idx, group["max_select"], frappe.bold(group["name"]))
			)

	row.modifiers = json.dumps(resolved, ensure_ascii=False) if resolved else None
	row.modifiers_text = "، ".join(texts) if texts else None
	row.modifiers_amount = extra
