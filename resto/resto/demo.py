"""Months of past restaurant activity on a copy of a site, to show the reports working (D-278).

Never on a live site: every call refuses unless the site's site_config.json has "resto_demo_site": 1.
Turn the copy's scheduler off first (bench --site X disable-scheduler).

	bench --site X execute resto.resto.demo.check
	bench --site X execute resto.resto.demo.reset --kwargs '{"confirm": "X"}'
	bench --site X execute resto.resto.demo.generate --kwargs '{"start": "2026-08-01", "end": "2026-11-25", "orders_per_day": 30, "seed": 1}'

Nothing is typed in here: the menu, recipes, tables, users, customers, suppliers, workers, accounts and
Resto Settings are read from the site. Each day is played through Resto's own calls, each at its time
of day (Frappe's clock is set back for the run): the cashier opens the shift with the drawer's cash,
the kitchen preps, guests order at lunch and dinner (dine in, takeaway, delivery), the kitchen starts,
readies and serves their tickets, the cashier settles them with the profile's payment methods, a few
get a discount, an item discount or a voided dish with the manager's PIN, the staff eat, a little is
wasted, the shift closes (a small over or short some days) and the cash goes to the main safe. Every
two or three days the kitchen is restocked from the suppliers for the days ahead, paid partly in cash
from the main safe and partly on credit, paid off weekly; salaries at each month's end, and the
monthly expenses given. The clone's opening entries are moved to the day before the first day.

Each day is one transaction, committed with its date as the last day done: a run stopped halfway
goes on from the next day when started again with the same arguments.
"""

import datetime
import json
import math
import random
import re
import traceback
from contextlib import contextmanager
from urllib.parse import unquote

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, get_first_day, get_last_day, getdate

MARK = "resto_demo_done_upto"
ARGS = "resto_demo_args"
PIN = "731905"  # the manager's approval PIN during the run; theirs is put back after it
PREPARED = "Prepared Ingredient"

# Each hour's share of the day's orders: the lunch and dinner peaks.
HOURS = {12: 1.0, 13: 3.0, 14: 4.0, 15: 3.0, 16: 1.5, 17: 1.0, 18: 1.5, 19: 3.0, 20: 4.0, 21: 3.5, 22: 1.5}
# Busier on Thursday and Friday (Monday = 0).
WEEKDAY = {0: 0.9, 1: 0.9, 2: 0.95, 3: 1.25, 4: 1.35, 5: 1.1, 6: 0.95}
LAST_PAY = "23:05"
GUEST_COUNTS = ([1, 2, 3, 4, 5, 6], [10, 35, 15, 25, 8, 7])
DRIVER = re.compile(r"driv|deliver|rider|سائق|توصيل|طيار|دليفري", re.I)

_resto_demo_clock = None  # read by Frappe's now_datetime during a run (see _patched)


def _clock_now_datetime():
	# Becomes the body of frappe.utils.data.now_datetime during a run; its names are looked up there.
	clock = _resto_demo_clock
	if clock is not None:
		return clock()
	return datetime.datetime.now(ZoneInfo(get_system_timezone())).replace(tzinfo=None)  # noqa: F821


def _guard():
	if not cint(frappe.conf.get("resto_demo_site")):
		frappe.throw("Demo data runs only on a copy of a site: set resto_demo_site to 1 in that site's site_config.json")


def _log(text):
	print(text, flush=True)


def _commit():
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: one day at a time, so a stopped run goes on from the next day


def _at(day, hm, minutes=0):
	h, m = (int(x) for x in hm.split(":"))
	return datetime.datetime.combine(getdate(day), datetime.time(h, m)) + datetime.timedelta(minutes=minutes)


# --------------------------------------------------------------------------- the clock


class _Clock:
	"""What Frappe reads as now: set at each step, and never going back (each read moves it on a
	little, so ledger entries keep the order they were made in)."""

	def __init__(self, start):
		self.now = start

	def set(self, when):
		if when > self.now:
			self.now = when

	def __call__(self):
		self.now = self.now + datetime.timedelta(microseconds=200)
		return self.now


def _quiet(*args, **kwargs):
	return None


def _no_printing(enqueue):
	"""Months of bills and tickets are not sent to the printers."""

	def run(method, *args, **kwargs):
		name = method if isinstance(method, str) else f"{getattr(method, '__module__', '')}.{getattr(method, '__qualname__', '')}"
		if name.startswith("resto.") and "print" in name:
			return None
		return enqueue(method, *args, **kwargs)

	return run


@contextmanager
def _patched(clock):
	import frappe.realtime
	from frappe.utils import data

	from resto.resto import shift as shifts

	code = data.now_datetime.__code__
	saved = (frappe.enqueue, frappe.publish_realtime, frappe.realtime.publish_realtime, shifts._fresh, frappe.flags.mute_emails)
	data._resto_demo_clock = clock
	# Every module holds the same function object (from frappe.utils import now_datetime): its body is
	# swapped, so nowdate, nowtime, now, today and getdate() all read the run's clock.
	data.now_datetime.__code__ = _clock_now_datetime.__code__
	frappe.enqueue = _no_printing(saved[0])
	frappe.publish_realtime = frappe.realtime.publish_realtime = _quiet  # nobody is watching the screens
	shifts._fresh = _quiet  # its commit would split a day; one process reads its own writes anyway
	frappe.flags.mute_emails = True
	try:
		yield
	finally:
		data.now_datetime.__code__ = code
		data._resto_demo_clock = None
		frappe.enqueue, frappe.publish_realtime, frappe.realtime.publish_realtime, shifts._fresh, frappe.flags.mute_emails = saved


# --------------------------------------------------------------------------- the manager's PIN


def _demo_pin(manager):
	from resto.resto import approval

	row = frappe.db.get_value("Resto Manager PIN", {"user": manager}, ["name", "pin_hash"], as_dict=True)
	if row:
		frappe.db.set_value("Resto Manager PIN", row.name, "pin_hash", approval._hash(PIN), update_modified=False)
		return row.name, row.pin_hash, False
	doc = frappe.new_doc("Resto Manager PIN")
	doc.user = manager
	doc.pin_hash = approval._hash(PIN)
	doc.flags.resto_pin = True
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name, None, True


def _restore_pin(name, old, created):
	try:
		if not frappe.db.exists("Resto Manager PIN", name):
			return
		if created:
			frappe.delete_doc("Resto Manager PIN", name, ignore_permissions=True, force=True)
		else:
			frappe.db.set_value("Resto Manager PIN", name, "pin_hash", old, update_modified=False)
	except Exception as e:
		_log(f"! the manager's PIN was not put back: {e}")


# --------------------------------------------------------------------------- the site


def _users(role):
	return [
		r[0]
		for r in frappe.db.sql(
			"""select distinct u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name and r.parenttype = 'User'
			where r.role = %s and u.enabled = 1 and u.name not in ('Guest', 'Administrator') order by u.name""",
			role,
		)
	]


class _Site:
	"""What the run reads from the site, once."""

	def __init__(self, seed):
		from resto.resto import floor
		from resto.resto import shift as shifts
		from resto.resto.utils import get_settings

		frappe.set_user("Administrator")
		s = self.settings = get_settings()
		self.company = s.company
		self.profile = s.pos_profile
		self.kitchen = s.kitchen_warehouse
		self.safe = s.get("main_safe_account")
		missing = [
			label
			for label, value in (
				("Company", self.company),
				("POS Profile", self.profile),
				("Kitchen Warehouse", self.kitchen),
				("Main Safe Account", self.safe),
			)
			if not value
		]
		if missing:
			frappe.throw("Resto Settings needs: " + ", ".join(missing))
		self.cost_center = frappe.db.get_value("POS Profile", self.profile, "cost_center") or frappe.get_cached_value(
			"Company", self.company, "cost_center"
		)

		profile_users = frappe.get_all("POS Profile User", {"parent": self.profile, "parenttype": "POS Profile"}, pluck="user")
		managers = _users("Resto Manager")
		cashiers = [u for u in _users("Resto Cashier") if not profile_users or u in profile_users]
		# A manager who may also take the cash approves; the drawer is the other cashiers' where there are any.
		self.cashiers = [u for u in cashiers if u not in managers] or cashiers
		if not self.cashiers:
			if profile_users and "Administrator" not in profile_users:
				frappe.throw(f"No user with the Resto Cashier role is a user of POS Profile {self.profile}")
			self.cashiers = ["Administrator"]
		# Resto lets a cashier take orders only on their own open shift: a waiter who is also a cashier
		# would need one. Only plain waiters wait tables; with none, the day's cashier takes the orders.
		self.waiters = [u for u in _users("Resto Waiter") if u not in set(_users("Resto Cashier")) | set(managers)]
		self.cooks = _users("Resto Kitchen") or ["Administrator"]
		self.manager = next((u for u in managers if u not in self.cashiers), "Administrator")

		self.modes = [m.mode_of_payment for m in shifts._modes(self.profile)]
		if not self.modes:
			frappe.throw(f"POS Profile {self.profile} has no payment methods")
		self.cash_modes = [m for m in self.modes if frappe.get_cached_value("Mode of Payment", m, "type") == "Cash"]
		self.main_cash = self.cash_modes[0] if self.cash_modes else None
		other = [m for m in self.modes if m != self.main_cash]
		# Most guests pay cash.
		self.pay_weights = [(m, 65 if m == self.main_cash else 35 / max(len(other), 1)) for m in self.modes] if self.main_cash else [(m, 1) for m in self.modes]

		skip_groups = set()
		if s.get("packaging_item_group"):
			lft, rgt = frappe.db.get_value("Item Group", s.packaging_item_group, ["lft", "rgt"])
			skip_groups = set(frappe.get_all("Item Group", filters={"lft": (">=", lft), "rgt": ("<=", rgt)}, pluck="name"))
		self.menu = [
			i
			for i in floor.get_menu()["items"]
			if flt(i["rate"]) > 0 and i["item_code"] != s.get("delivery_fee_item") and i["item_group"] not in skip_groups
		]
		if not self.menu:
			frappe.throw("The menu has no priced dishes")
		# Some dishes sell much more than others: a fixed order of favourites for the seed.
		ranked = sorted(self.menu, key=lambda i: i["item_code"])
		random.Random(f"{seed}|menu").shuffle(ranked)
		self.dish_weights = [1 / (n + 1) ** 0.8 for n in range(len(ranked))]
		self.ranked = ranked
		self.cheap = sorted(self.menu, key=lambda i: flt(i["rate"]))[: max(3, len(self.menu) // 5)]

		from resto.resto.doctype.resto_modifier_group.resto_modifier_group import groups_for_item

		self.mod_groups = {i["item_code"]: groups_for_item(i["item_code"]) for i in self.menu}
		self.tables = frappe.get_all("Resto Table", filters={"is_active": 1}, fields=["name", "seats"], order_by="name")
		walk_in = frappe.db.get_value("POS Profile", self.profile, "customer")
		self.customers = [
			c
			for c in frappe.get_all(
				"Customer", filters={"disabled": 0, "mobile_no": ("is", "set")}, fields=["name", "resto_address"], order_by="name", limit=1000
			)
			if c.name != walk_in
		]
		self.employees = frappe.get_all(
			"Employee", filters={"company": self.company, "status": "Active"}, fields=["name", "employee_name", "designation"], order_by="name"
		)
		self.drivers = [e.name for e in self.employees if DRIVER.search(e.designation or "")]
		cash_supplier = s.get("cash_supplier")
		suppliers = frappe.get_all("Supplier", filters={"disabled": 0}, pluck="name", order_by="name")
		self.suppliers = [x for x in suppliers if x != cash_supplier] or suppliers
		if not self.suppliers:
			frappe.throw("Add at least one supplier")
		self.seed = seed
		self._load_recipes()
		self._rates, self._supplier, self._uoms = {}, {}, {}

	def _load_recipes(self):
		from resto.resto.doctype.resto_prep.resto_prep import _get_bom

		self.bundles = {}
		for r in frappe.get_all("Product Bundle Item", fields=["parent", "item_code", "qty", "uom"], order_by="parent, idx"):
			factor = 1
			stock_uom = frappe.get_cached_value("Item", r.item_code, "stock_uom")
			if r.uom and r.uom != stock_uom:
				factor = flt(frappe.db.get_value("UOM Conversion Detail", {"parent": r.item_code, "uom": r.uom}, "conversion_factor")) or 1
			self.bundles.setdefault(r.parent, []).append((r.item_code, flt(r.qty) * factor))
		self.prepared = {}
		for r in frappe.get_all(
			"Resto Recipe",
			filters={"recipe_type": PREPARED, "is_active": 1, "company": self.company},
			fields=["name", "item", "par_qty"],
		):
			try:
				bom = frappe.get_doc("BOM", _get_bom(r.name, r.item))
			except frappe.ValidationError:
				continue
			per = {d.item_code: flt(d.stock_qty) / (flt(bom.quantity) or 1) for d in bom.items}
			self.prepared[r.item] = frappe._dict(recipe=r.name, par=flt(r.par_qty), per=per)

	# ---- what dishes take

	def parts(self, item_code, qty, raw, prep, depth=0):
		"""Stock a dish (or a paid option) takes: its bundle's parts, down to stock items; the
		prepared ones apart (the kitchen makes them)."""
		if depth > 6 or qty <= 0:
			return
		if item_code in self.bundles:
			for child, per in self.bundles[item_code]:
				self.parts(child, qty * per, raw, prep, depth + 1)
		elif item_code in self.prepared:
			prep[item_code] = prep.get(item_code, 0) + qty
		elif cint(frappe.get_cached_value("Item", item_code, "is_stock_item")):
			raw[item_code] = raw.get(item_code, 0) + qty

	def prep_raw(self, item_code, qty, raw, depth=0):
		"""What making qty of a prepared item takes (a prepared part of it, made the same way)."""
		if depth > 4 or qty <= 0:
			return
		for part, per in self.prepared[item_code].per.items():
			if part in self.prepared:
				self.prep_raw(part, qty * per, raw, depth + 1)
			elif cint(frappe.get_cached_value("Item", part, "is_stock_item")):
				raw[part] = raw.get(part, 0) + qty * per

	def level(self, item_code, depth=0):
		"""0 for a prepared item made of stock items only; one more than its deepest prepared part."""
		cache = self.__dict__.setdefault("_levels", {})
		if item_code not in cache:
			parts = [k for k in self.prepared[item_code].per if k in self.prepared and k != item_code]
			cache[item_code] = 1 + max(self.level(k, depth + 1) for k in parts) if parts and depth < 8 else 0
		return cache[item_code]

	def by_level(self, items, deepest_first=False):
		return sorted(items, key=lambda i: (self.level(i) * (-1 if deepest_first else 1), i))

	def order_parts(self, spec, raw, prep):
		for line in spec.lines:
			self.parts(line.item_code, line.qty, raw, prep)
			for mod_item in line.mod_items:
				self.parts(mod_item, line.qty, raw, prep)
		for item_code, qty in self.packaging(spec).items():
			raw[item_code] = raw.get(item_code, 0) + qty

	def packaging(self, spec):
		"""The packaging and consumables the order's bill takes (Resto's own rules: each dish's
		packaging for its order type, and the order type's automatic consumables)."""
		from resto.resto import packaging

		key = (spec.kind, spec.guests, tuple((x.item_code, x.qty) for x in spec.lines))
		cache = self.__dict__.setdefault("_packaging", {})
		if key not in cache:
			need = {}
			for line in spec.lines:
				for item_code, qty in packaging.per_dish(line.item_code, spec.kind).items():
					need[item_code] = need.get(item_code, 0) + qty * line.qty
			for item_code, qty in packaging.per_order(frappe._dict(order_type=spec.kind, guests=spec.guests)).items():
				need[item_code] = need.get(item_code, 0) + qty
			cache[key] = {k: (math.ceil(v - 1e-9) if self.whole(k) else v) for k, v in need.items() if v > 0}
		return cache[key]

	# ---- buying

	def rate(self, item_code):
		"""What the item costs, from the site: its value in the kitchen, the item, its last purchase."""
		if item_code not in self._rates:
			rate = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.kitchen}, "valuation_rate"))
			if not rate:
				v = frappe.get_cached_value("Item", item_code, ["valuation_rate", "last_purchase_rate"], as_dict=True) or {}
				rate = flt(v.get("valuation_rate")) or flt(v.get("last_purchase_rate"))
			if not rate:
				row = frappe.db.sql(
					"""select valuation_rate from `tabStock Ledger Entry` where item_code = %s and is_cancelled = 0
					and valuation_rate > 0 order by posting_date desc, posting_time desc, creation desc limit 1""",
					item_code,
				)
				rate = flt(row[0][0]) if row else 0
			if not rate:
				rate = flt(frappe.db.get_value("Item Price", {"item_code": item_code, "buying": 1}, "price_list_rate"))
			self._rates[item_code] = rate
		return self._rates[item_code]

	def supplier(self, item_code):
		"""The item's supplier on the site (its default, its supplier list, its last purchase), or one of them."""
		if item_code not in self._supplier:
			found = frappe.db.get_value("Item Default", {"parent": item_code, "company": self.company}, "default_supplier")
			found = found or frappe.db.get_value("Item Supplier", {"parent": item_code}, "supplier")
			if not found:
				row = frappe.db.sql(
					"""select pi.supplier from `tabPurchase Invoice Item` d join `tabPurchase Invoice` pi on pi.name = d.parent
					where d.item_code = %s and pi.docstatus = 1 order by pi.posting_date desc limit 1""",
					item_code,
				)
				found = row[0][0] if row else None
			if not found or not frappe.db.exists("Supplier", {"name": found, "disabled": 0}):
				group = frappe.get_cached_value("Item", item_code, "item_group") or ""
				found = random.Random(f"{self.seed}|{group}").choice(self.suppliers)
			self._supplier[item_code] = found
		return self._supplier[item_code]

	def whole(self, item_code):
		"""Counted in whole units (pieces), not weighed."""
		if item_code not in self._uoms:
			uom = frappe.get_cached_value("Item", item_code, "stock_uom")
			self._uoms[item_code] = cint(frappe.get_cached_value("UOM", uom, "must_be_whole_number"))
		return self._uoms[item_code]

	def round_up(self, item_code, qty):
		if qty <= 0:
			return 0
		if self.whole(item_code):
			return math.ceil(qty - 1e-9)
		step = 0.5 if qty >= 5 else 0.05
		return round(math.ceil(qty / step - 1e-9) * step, 3)

	def stock(self, item_code):
		return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.kitchen}, "actual_qty"))


# --------------------------------------------------------------------------- the plan of a day


class _Planner:
	"""The orders of each day, the same for the same arguments (the seed), whatever ran before."""

	def __init__(self, site, start, end, per_day, seed):
		self.site, self.start, self.end, self.per_day, self.seed = site, getdate(start), getdate(end), per_day, seed
		self._plans = {}
		# The kitchen is restocked every two or three days.
		rng = random.Random(f"{seed}|restock")
		self.restock_days, day = [], self.start
		while day <= add_days(self.end, 4):
			self.restock_days.append(day)
			day = add_days(day, rng.choice([2, 3]))

	def next_restock(self, day):
		return next((d for d in self.restock_days if d > getdate(day)), add_days(day, 3))

	def plan(self, day):
		day = getdate(day)
		if day not in self._plans:
			self._plans[day] = self._make(day)
		return self._plans[day]

	def _make(self, day):
		site = self.site
		rng = random.Random(f"{self.seed}|{day}")
		# Busier as the months go (over four months, whatever the end: a longer run keeps the days done).
		trend = 0.85 + 0.3 * min(max(date_diff(day, self.start), 0), 120) / 120
		count = max(1, round(self.per_day * WEEKDAY[day.weekday()] * trend * rng.uniform(0.85, 1.15)))
		hours, weights = list(HOURS), list(HOURS.values())
		specs = []
		for _n in range(count):
			kind = rng.choices(["Dine In", "Takeaway", "Delivery"], weights=[60, 30, 10])[0]
			hour = rng.choices(hours, weights=weights)[0]
			minute = rng.randint(0, 59)
			if kind == "Dine In" and (hour, minute) > (21, 30):
				kind = "Takeaway"  # too late to sit and eat before the close
			if kind == "Delivery" and hour == 22:
				minute = min(minute, 20)
			if hour == 22:
				minute = min(minute, 40)
			at = _at(day, f"{hour}:{minute}") + datetime.timedelta(seconds=rng.randint(0, 59))
			specs.append(self._order(rng, day, at, kind))
		specs.sort(key=lambda s: s.at)
		self._seat(specs, day)
		limit = site.settings.get("cashier_discount_limit")
		for spec in specs:
			if spec.discount:
				spec.discount = min(spec.discount, flt(limit)) if flt(limit) else spec.discount
		staff = None
		if site.employees and rng.random() < 0.6:
			staff = frappe._dict(
				at=_at(day, "17:30", rng.randint(0, 40)),
				staff_for=rng.choice(site.employees).employee_name,
				items=[{"item_code": i["item_code"], "qty": 1} for i in rng.sample(site.cheap, min(len(site.cheap), rng.randint(1, 3)))],
			)
		over_short = rng.choice([-50, -20, -10, -5, 5, 10, 20]) if rng.random() < 0.15 else 0
		return frappe._dict(orders=specs, staff=staff, over_short=over_short, waste_seed=rng.random())

	def _order(self, rng, day, at, kind):
		site = self.site
		guests = rng.choices(*GUEST_COUNTS)[0] if kind == "Dine In" else 1
		if kind == "Dine In":
			n = min(guests + rng.randint(0, guests), 10)
		elif kind == "Takeaway":
			n = rng.randint(1, 3)
		else:
			n = rng.randint(2, 5)
		lines = {}
		for _n in range(n):
			dish = rng.choices(site.ranked, weights=site.dish_weights)[0]
			mods, mod_items = self._mods(rng, dish["item_code"])
			key = (dish["item_code"], json.dumps(mods, sort_keys=True))
			if key in lines:
				lines[key].qty += 1
			else:
				lines[key] = frappe._dict(item_code=dish["item_code"], qty=1, modifiers=mods, mod_items=mod_items)
		lines = list(lines.values())
		spec = frappe._dict(
			at=at,
			kind=kind,
			guests=guests,
			lines=lines,
			waiter=rng.choice(site.waiters) if site.waiters else None,
			table=None,
			customer=None,
			driver=None,
			discount=rng.choice([5, 10, 15, 20]) if rng.random() < 0.06 else 0,
			line_discount=None,
			void=None,
			pay=rng.choices([m for m, _w in site.pay_weights], weights=[w for _m, w in site.pay_weights])[0],
			split=None,
		)
		if len(lines) >= 2 and rng.random() < 0.03:
			spec.line_discount = (rng.randrange(len(lines)), rng.choice([20, 50, 100]))
		if len(lines) >= 2 and rng.random() < 0.03:
			spec.void = rng.randrange(len(lines))
			if spec.line_discount and spec.line_discount[0] == spec.void:
				spec.line_discount = None
		if len(site.modes) > 1 and rng.random() < 0.08:
			second = rng.choice([m for m in site.modes if m != spec.pay])
			spec.split = (second, round(rng.uniform(0.3, 0.7), 2))
		if site.customers and (kind == "Delivery" and rng.random() < 0.85 or kind == "Takeaway" and rng.random() < 0.25):
			spec.customer = rng.choice(site.customers)
		if kind == "Delivery" and site.drivers:
			spec.driver = rng.choice(site.drivers)
		self._times(rng, spec, day)
		return spec

	def _mods(self, rng, item_code):
		"""The choices a dish must have (its defaults first), and now and then a paid extra."""
		mods, mod_items = [], []
		for g in self.site.mod_groups.get(item_code) or []:
			options = g["options"]
			if not options:
				continue
			picked = []
			if g["min_select"]:
				ordered = sorted(options, key=lambda o: (not o["is_default"], rng.random()))
				picked = ordered[: g["min_select"]]
			elif rng.random() < 0.06:
				paid = [o for o in options if flt(o["price"]) and o["item"]]
				if paid:
					picked = [rng.choice(paid)]
			for o in picked:
				mods.append({"group": g["name"], "option": o["option_name"]})
				if flt(o["price"]) and o["item"]:
					mod_items.append(o["item"])
		return mods, mod_items

	def _times(self, rng, spec, day):
		"""Minutes after the order: the kitchen starts, it is ready, served, eaten, paid."""
		limit = (_at(day, LAST_PAY) - spec.at).total_seconds() / 60
		t = frappe._dict(start=rng.uniform(1, 4))
		t.ready = t.start + rng.uniform(8, 18) + 1.2 * len(spec.lines)
		if spec.kind == "Dine In":
			t.served = t.ready + rng.uniform(1, 4)
			t.bill = t.served + min(rng.uniform(25, 55), max(limit - t.served - 8, 5))
			t.pay = t.bill + rng.uniform(3, 6)
		elif spec.kind == "Takeaway":
			t.pay = t.ready + rng.uniform(1, 5)
		else:
			t.out = t.ready + rng.uniform(2, 6)
			t.pay = t.out + min(rng.uniform(15, 35), max(limit - t.out - 1, 5))
		spec.t = t

	def _seat(self, specs, day):
		"""A free table for each dine-in party, the smallest that seats it; none free, they take it away."""
		busy = {t.name: [] for t in self.site.tables}
		seats = {t.name: cint(t.seats) or 4 for t in self.site.tables}
		for spec in specs:
			if spec.kind != "Dine In":
				continue
			start, end = spec.at, spec.at + datetime.timedelta(minutes=spec.t.pay + 3)
			free = [n for n, spans in busy.items() if all(end <= a or start >= b for a, b in spans)]
			fits = sorted((n for n in free if seats[n] >= spec.guests), key=lambda n: (seats[n], n))
			table = fits[0] if fits else (sorted(free)[0] if free else None)
			if not table:
				spec.kind, spec.guests = "Takeaway", 1
				self._times(random.Random(f"{self.seed}|{spec.at}"), spec, day)
				continue
			spec.table = table
			busy[table].append((start, end))


# --------------------------------------------------------------------------- playing a day


class _Run:
	def __init__(self, site, planner, clock, change_float, monthly):
		self.site, self.planner, self.clock = site, planner, clock
		self.monthly = monthly or []
		self.lang = frappe.db.get_single_value("System Settings", "language") or "en"
		avg = sum(flt(i["rate"]) for i in site.menu) / len(site.menu)
		# The change kept in the drawer: a few dishes' worth unless given.
		self.float = flt(change_float) if change_float is not None else round(avg * 5 / 100) * 100
		self.sp = 0
		self.warned = set()
		self.stats = {}

	# ---- steps

	def as_user(self, user):
		frappe.set_user(user)
		frappe.local.lang = self.lang

	def do(self, when, user, fn, label, state=None, critical=False):
		if state is not None and state.get("failed"):
			return None
		self.clock.set(when)
		self.as_user(user)
		frappe.flags.resto_approvals = {}
		try:
			return self.attempt(fn)
		except Exception as e:
			if state is not None:
				state["failed"] = True
			self.count("failed")
			text = re.sub(r"<[^>]+>", "", str(e))[:300]
			_log(f"  ! {when:%H:%M} {label} ({frappe.session.user}): {type(e).__name__}: {text}")
			if not isinstance(e, frappe.ValidationError) or isinstance(e, frappe.PermissionError):
				_log("    " + "    ".join(traceback.format_exception(e)[-7:]))
			if critical:
				raise
			return None
		finally:
			frappe.local.message_log = []

	def attempt(self, fn, depth=0):
		"""Run a step; short of stock, the kitchen gets it (bought, or prepared) and the step runs again."""
		for _try in range(6):
			self.sp += 1
			name = f"resto_demo_{self.sp}"
			frappe.db.savepoint(name)
			try:
				return fn()
			except Exception as e:
				frappe.db.rollback(save_point=name)
				short = _short_of(e)
				if not short or depth > 2:
					raise
				self.top_up(*short, depth=depth)
		raise frappe.ValidationError("Still short of stock after topping it up")

	def top_up(self, item_code, qty, depth=0):
		user = frappe.session.user
		self.as_user(self.site.manager)
		try:
			use = self.daily_use().get(item_code, 0)
			need = max(qty * 1.2, qty + 2 * use, qty + 0.001)
			if item_code in self.site.prepared:
				self.attempt(lambda: self.prep({item_code: need + max(self.site.stock(item_code), 0)}, urgent=True), depth + 1)
			else:
				self.attempt(lambda: self.buy({item_code: need}, urgent=True), depth + 1)
			self.count("top_ups")
		finally:
			self.as_user(user)

	def count(self, key, by=1):
		self.stats[key] = self.stats.get(key, 0) + by

	def warn_once(self, text):
		if text not in self.warned:
			self.warned.add(text)
			_log(f"  ! {text}")

	# ---- the day

	def day(self, day):
		site, day = self.site, getdate(day)
		self.rng = random.Random(f"{self.planner.seed}|run|{day}")
		self.stats = {}
		plan = self.planner.plan(day)
		cashier = site.cashiers[date_diff(day, self.planner.start) % len(site.cashiers)]
		self.cashier = cashier
		events = []

		def add(when, user, fn, label, state=None, critical=False):
			events.append((when, len(events), user, fn, label, state, critical))

		if day in self.planner.restock_days:
			add(_at(day, "10:00"), site.manager, lambda: self.restock(day), "restock")
		if date_diff(day, self.planner.start) % 7 == 6:
			add(_at(day, "10:30"), site.manager, self.pay_suppliers, "supplier payments")
		add(_at(day, "11:30"), cashier, self.open_shift, "open shift", critical=True)
		add(_at(day, "11:45"), site.manager, lambda: self.prep(self.prep_need(plan)), "prep")
		# Before the evening: what the rest of the day's orders take, less what is left (more was used
		# than planned, a recipe changed), and any balance below zero.
		evening = _at(day, "17:15")
		add(
			evening,
			site.manager,
			lambda: self.prep(self.prep_need(plan, after=evening), suggest=0, extra=1.05, remark=_("Prepared for the evening")),
			"evening prep",
		)
		for spec in plan.orders:
			self.order_steps(spec, cashier, add)
		if plan.staff and site.settings.get("staff_meal_account"):
			add(plan.staff.at, cashier, lambda: self.staff_meal(plan.staff), "staff meal")
		for m in self.monthly:
			# Due on its day; not paid yet (the safe was short), it is tried again each day of the month.
			if day.day >= min(cint(m.get("day") or 1), get_last_day(day).day):
				add(_at(day, "12:30"), site.manager, lambda m=m: self.monthly_expense(m), "monthly expense")
		if day == get_last_day(day):
			add(_at(day, "16:00"), site.manager, lambda: self.salaries(day), "salaries")
		elif day.day <= 15 and date_diff(day, self.planner.start) >= day.day:
			# Last month's salaries, if the safe could not pay them at its end.
			add(_at(day, "16:00"), site.manager, lambda: self.salaries(add_days(get_first_day(day), -1)), "salaries")
		add(_at(day, "22:45"), site.manager, lambda: self.waste(plan.waste_seed), "waste")
		add(_at(day, "23:12"), cashier, self.leftovers, "orders left open")
		add(_at(day, "23:20"), cashier, lambda: self.close_shift(plan.over_short), "close shift", critical=True)
		add(_at(day, "23:25"), cashier, self.cash_drop, "cash drop")
		events.sort(key=lambda e: (e[0], e[1]))
		for when, _n, user, fn, label, state, critical in events:
			self.do(when, user, fn, label, state, critical)
		s = self.stats
		_log(
			f"{day}: {s.get('paid', 0)}/{len(plan.orders)} orders paid, sales {flt(s.get('sales', 0), 2)}, "
			f"{s.get('purchases', 0)} purchases, {s.get('top_ups', 0)} top-ups, {s.get('failed', 0)} failed steps"
		)
		if plan.orders and not s.get("paid"):
			# Something every order trips on (no price that day, a missing account): fix it, not a week of failures.
			frappe.throw(f"No order was paid on {day}: stopped, the day not kept (see the failed steps above)")

	# ---- the shift

	def open_shift(self):
		from resto.resto import cash_guard, opening_check
		from resto.resto import shift as shifts

		site = self.site
		amounts, extra = {}, None
		for mode in site.modes:
			amounts[mode] = 0
			if mode in site.cash_modes:
				account = opening_check._account(mode, site.company)
				amounts[mode] = max(flt(cash_guard.balance(account, site.company, frappe.utils.nowdate()), 2), 0) if account else 0
		if site.main_cash and amounts[site.main_cash] < self.float and self.safe_balance() >= self.float - amounts[site.main_cash]:
			amounts[site.main_cash], extra = self.float, "change"  # change from the main safe
		self.shift = shifts.open_shift(json.dumps(amounts), extra=extra, approver=site.manager, pin=PIN)

	def close_shift(self, over_short):
		from resto.resto import shift as shifts

		doc = frappe.get_doc("Resto Shift", self.shift)
		amounts = {r["mode_of_payment"]: flt(r["expected_amount"], 2) for r in shifts.compute(doc)["rows"]}
		main = self.site.main_cash
		if over_short and main in amounts:
			amounts[main] = max(flt(amounts[main] + over_short, 2), 0)
		shifts.close_shift(json.dumps(amounts))

	def cash_drop(self):
		from resto.resto import drawer_cash

		data = drawer_cash.drop_data()
		for mode, left in (data.get("left") or {}).items():
			amount = flt(left - (self.float if mode == self.site.main_cash else 0), 2)
			if amount > 0.5:
				drawer_cash.cash_drop(amount, mode, request_id=f"demo-{self.shift}-{mode}", approver=self.site.manager, pin=PIN)

	def leftovers(self):
		"""An order still open at the close (a step of it failed) is cancelled, and no ticket is left on
		the kitchen board."""
		from resto.resto import floor, kitchen
		from resto.resto.doctype.resto_order.resto_order import ACTIVE

		for name in frappe.get_all("Resto Order", filters={"status": ("in", ACTIVE), "pos_profile": self.site.profile}, pluck="name"):
			try:
				self.attempt(lambda name=name: floor.cancel_order(name, _("Guest left before paying"), cooked=0, approver=self.site.manager, pin=PIN))
			except Exception as e:
				_log(f"  ! open order {name} not cancelled: {re.sub(r'<[^>]+>', '', str(e))[:200]}")
		for ticket in frappe.get_all(
			"Resto Kitchen Ticket", filters={"status": ("in", ("New", "In Progress", "Ready"))}, pluck="name", order_by="name"
		):
			try:
				self.attempt(lambda ticket=ticket: kitchen.set_ticket_status(ticket, "Served"))
			except Exception:
				pass  # a void ticket, or one that cannot be served: left as it is

	# ---- an order

	def order_steps(self, spec, cashier, add):
		from resto.resto import floor, kitchen

		site = self.site
		st = {}
		t = spec.t
		waiter = spec.waiter or cashier
		taker = waiter if spec.kind == "Dine In" else cashier
		at = lambda minutes: spec.at + datetime.timedelta(minutes=minutes)  # noqa: E731

		def tickets(statuses):
			return frappe.get_all(
				"Resto Kitchen Ticket",
				filters={"resto_order": st["order"], "ticket_type": "Order", "status": ("in", statuses)},
				pluck="name",
				order_by="name",
			)

		def lines():
			return frappe.get_all(
				"Resto Order Item",
				filters={"parent": st["order"], "parenttype": "Resto Order", "kitchen_status": ("!=", "Void")},
				pluck="name",
				order_by="idx",
			)

		def create():
			c = spec.customer
			name = floor.create_order(
				order_type=spec.kind,
				table=spec.table,
				guests=spec.guests,
				customer=c.name if c else None,
				address=(c.resto_address or None) if c and spec.kind == "Delivery" else None,
			)
			floor.add_items(name, json.dumps([{"item_code": x.item_code, "qty": x.qty, "modifiers": x.modifiers} for x in spec.lines]))
			floor.send_to_kitchen(name)
			# Printed back then: a kitchen screen that prints does not print months of tickets now.
			frappe.db.sql("update `tabResto Kitchen Ticket` set printed = 1 where resto_order = %s", name)
			st["order"] = name

		def void():
			rows = lines()
			if len(rows) > 1:
				floor.void_line(st["order"], rows[min(spec.void, len(rows) - 1)], _("Guest changed their mind"), cooked=0, approver=site.manager, pin=PIN)

		def cook(status, statuses):
			for ticket in tickets(statuses):
				kitchen.set_ticket_status(ticket, status)

		def line_discount():
			rows = lines()
			idx, pct = spec.line_discount
			floor.set_line_discount(st["order"], rows[min(idx, len(rows) - 1)], percentage=pct, approver=site.manager, pin=PIN)

		def discount():
			floor.apply_discount(st["order"], percentage=spec.discount, approver=site.manager, pin=PIN)

		def pay():
			# The bill takes its stock now: what the kitchen lacks of it is bought first (the error
			# of a short item reads differently by item and version; this does not depend on it).
			raw = {}
			site.order_parts(spec, raw, {})
			for item_code, qty in raw.items():
				short = qty - site.stock(item_code)
				if short > 1e-6:
					self.top_up(item_code, short)
			payable = flt(floor.get_bill(st["order"])["payable"], 2)
			if spec.split and payable > 1:
				first = flt(payable * spec.split[1], 2)
				payments = [{"mode_of_payment": spec.pay, "amount": first}, {"mode_of_payment": spec.split[0], "amount": flt(payable - first, 2)}]
			else:
				payments = [{"mode_of_payment": spec.pay, "amount": payable}]
			floor.settle(st["order"], json.dumps(payments), request_id=f"demo-{st['order']}")
			self.count("paid")
			self.count("sales", payable)

		add(spec.at, taker, create, f"order at {spec.at:%H:%M}", st)
		if spec.void is not None:
			add(at(t.start * 0.5), taker, void, "void", st)
		add(at(t.start), site.cooks[0] if len(site.cooks) == 1 else self.rng.choice(site.cooks), lambda: cook("In Progress", ["New"]), "kitchen start", st)
		add(at(t.ready), site.cooks[0], lambda: cook("Ready", ["New", "In Progress"]), "kitchen ready", st)
		if spec.kind == "Dine In":
			add(at(t.served), waiter, lambda: floor.mark_served(st["order"]), "served", st)
			if spec.line_discount:
				add(at(t.served + 1), cashier, line_discount, "item discount", st)
			if spec.discount:
				add(at(t.served + 2), cashier, discount, "discount", st)
			add(at(t.bill), waiter, lambda: floor.request_bill(st["order"]), "bill", st)
		else:
			if spec.line_discount:
				add(at(t.ready + 0.5), cashier, line_discount, "item discount", st)
			if spec.discount:
				add(at(t.ready + 0.6), cashier, discount, "discount", st)
		if spec.kind == "Delivery":
			add(at(t.out), cashier, lambda: floor.set_delivery(st["order"], "Out for Delivery", driver=spec.driver), "out for delivery", st)
		add(at(t.pay), cashier, pay, "payment", st)
		if spec.kind == "Delivery":
			add(at(t.pay + 0.2), cashier, lambda: floor.set_delivery(st["order"], "Delivered"), "delivered", st)

	def staff_meal(self, meal):
		from resto.resto import floor, staff_meal

		name = staff_meal.create(staff_for=meal.staff_for)
		floor.add_items(name, json.dumps(meal["items"]))
		staff_meal.post(name)
		frappe.db.sql("update `tabResto Kitchen Ticket` set printed = 1 where resto_order = %s", name)
		self.count("staff_meals")

	# ---- the kitchen's stock

	def prep_need(self, plan, after=None):
		"""The prepared items the day's orders (those after a time, if given) and staff meal take."""
		raw, prep = {}, {}
		for spec in plan.orders:
			if not after or spec.at >= after:
				self.site.order_parts(spec, raw, prep)
		if plan.staff and (not after or plan.staff.at >= after):
			for i in plan.staff["items"]:
				self.site.parts(i["item_code"], flt(i["qty"]), raw, prep)
		return prep

	def prep(self, need, urgent=False, suggest=None, extra=1.1, remark=None):
		"""A prep: the par levels (Resto Prep's own suggestions, unless suggest=0), and what is still
		short of need (times extra) in the kitchen. A prepared item made of another (a sauce made
		with a stock, a mix used in a dish's filling) needs that one made too: worked out from the
		top level down. A balance below zero (prepared items may go below it) is made up as well."""
		site = self.site
		if not site.prepared:
			return
		if urgent:
			extra = 1
		doc = frappe.new_doc("Resto Prep")
		doc.company = site.company
		doc.posting_date = frappe.utils.nowdate()
		doc.warehouse = site.kitchen
		doc.flags.ignore_permissions = True
		if (not urgent) if suggest is None else cint(suggest):
			doc.get_suggestions()
		rows = {r.item_code: r for r in doc.items}
		want = {i: flt(q) * extra for i, q in need.items() if i in site.prepared}
		# Every prepared item, the top level first: a part is reached after all that use it.
		for item_code in site.by_level(site.prepared, deepest_first=True):
			row = rows.get(item_code)
			short = want.get(item_code, 0) - site.stock(item_code)
			qty = site.round_up(item_code, max(flt(row.qty) if row else 0, short))
			if qty <= 0:
				if row:
					row.qty = 0
				continue
			if not row:
				row = rows[item_code] = doc.append("items", {"recipe": site.prepared[item_code].recipe, "item_code": item_code, "qty": 0})
			row.qty = qty
			for part, per in site.prepared[item_code].per.items():
				if part in site.prepared and part != item_code:
					want[part] = want.get(part, 0) + qty * per
		if not any(flt(r.qty) > 0 for r in doc.items):
			return
		if remark or urgent:
			doc.remarks = remark or _("Prepared again during the service")
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()

	def daily_use(self):
		"""What the kitchen used a day over the last week (sales, prep, staff meals, waste)."""
		today = getdate(frappe.utils.nowdate())
		days = min(7, date_diff(today, self.planner.start))
		if days <= 0:
			return {}
		rows = frappe.db.sql(
			"""select item_code, -sum(actual_qty) from `tabStock Ledger Entry`
			where warehouse = %s and is_cancelled = 0 and actual_qty < 0 and posting_date >= %s and posting_date < %s
			and voucher_type in ('Sales Invoice', 'POS Invoice', 'Stock Entry') group by item_code""",
			(self.site.kitchen, add_days(today, -days), today),
		)
		return {r[0]: flt(r[1]) / days for r in rows}

	def restock(self, day):
		"""Buy for the days until the next delivery and one more: what their orders take (and the prep
		they need, with the par levels kept), or what the kitchen used a day lately if that is more."""
		site = self.site
		upto = self.planner.next_restock(day)
		days = [add_days(day, n) for n in range(date_diff(upto, day) + 1)]
		raw, prep = {}, {}
		for d in days:
			for spec in self.planner.plan(d).orders:
				site.order_parts(spec, raw, prep)
		for item_code, p in site.prepared.items():
			# prep_raw goes down into the prepared parts: what a sauce takes of its stock is bought too.
			site.prep_raw(item_code, prep.get(item_code, 0) + p.par + max(-site.stock(item_code), 0), raw)
		use = self.daily_use()
		needs = {}
		for item_code in set(raw) | set(use):
			if item_code in site.prepared:
				continue
			target = max(raw.get(item_code, 0), use.get(item_code, 0) * len(days)) * 1.15
			short = target - max(site.stock(item_code), 0)
			if short > 0.0001:
				needs[item_code] = short
		self.buy(needs)

	def buy(self, needs, urgent=False):
		site = self.site
		by_supplier = {}
		for item_code, qty in sorted(needs.items()):
			rate = site.rate(item_code)
			if not rate:
				self.warn_once(f"no cost known for {item_code}: not bought")
				continue
			qty = site.round_up(item_code, qty)
			by_supplier.setdefault(site.supplier(item_code), []).append((item_code, qty, flt(rate * self.rng.uniform(0.97, 1.04), 2)))
		for supplier, rows in by_supplier.items():
			share = 1 if urgent else self.rng.choice([1, 1, 0.5, 0, 0])
			self.purchase(supplier, rows, share, urgent)

	def purchase(self, supplier, rows, share, urgent):
		"""Received into the kitchen; paid in cash from the main safe (all, part or none of it: the rest on credit)."""
		site = self.site
		pi = frappe.new_doc("Purchase Invoice")
		pi.flags.ignore_permissions = True
		pi.update(
			{
				"company": site.company,
				"supplier": supplier,
				"posting_date": frappe.utils.nowdate(),
				"posting_time": frappe.utils.nowtime(),
				"set_posting_time": 1,
				"update_stock": 1,
				"set_warehouse": site.kitchen,
				"cost_center": site.cost_center,
				"ignore_pricing_rule": 1,
				"disable_rounded_total": 1,
				"remarks": _("Urgent purchase for the kitchen") if urgent else _("Kitchen restock"),
			}
		)
		for item_code, qty, rate in rows:
			uom = frappe.get_cached_value("Item", item_code, "stock_uom")
			pi.append(
				"items",
				{
					"item_code": item_code,
					"qty": qty,
					"uom": uom,
					"stock_uom": uom,
					"conversion_factor": 1,
					"rate": rate,
					"warehouse": site.kitchen,
					"cost_center": site.cost_center,
				},
			)
		pi.set_missing_values()
		if pi.meta.has_field("apply_tds"):
			pi.apply_tds = 0
		pi.taxes_and_charges = None
		pi.set("taxes", [])
		for d, (_item, _qty, rate) in zip(pi.items, rows):
			d.price_list_rate = d.rate = rate
			d.discount_percentage = d.discount_amount = 0
		pi.calculate_taxes_and_totals()
		paid = min(flt(pi.grand_total * share, 2), flt(self.safe_balance() - 1, 2))
		if paid > 0:
			pi.is_paid = 1
			pi.cash_bank_account = site.safe
			pi.paid_amount = pi.base_paid_amount = paid
		pi.insert()
		pi.submit()
		self.count("purchases")

	def waste(self, seed):
		"""A little of what is in the kitchen is wasted: a prepared item left over, something spoiled."""
		site = self.site
		if not site.settings.get("waste_account"):
			return self.warn_once("Resto Settings has no Food Waste Account: no waste")
		rng = random.Random(seed)
		stock = frappe.db.sql(
			"select item_code, actual_qty from `tabBin` where warehouse = %s and actual_qty > 0 order by item_code", site.kitchen
		)
		if not stock:
			return
		from resto.resto.units import _precision

		# Resto Waste keeps the quantity to the stock precision: a smaller amount would be 0 there.
		precision = _precision()
		rows = []
		for item_code, qty in rng.sample(list(stock), min(len(stock), rng.randint(1, 2))):
			if site.whole(item_code):
				lost = 1 if qty >= 4 else 0
			else:
				lost = flt(flt(qty) * rng.uniform(0.01, 0.04), precision)
			if lost <= 0:
				continue
			reasons = ["Over Production", "Spoiled", "Expired"] if item_code in site.prepared else ["Spoiled", "Expired", "Dropped", "Burnt"]
			rows.append({"item_code": item_code, "qty": lost, "reason": rng.choice(reasons)})
		if not rows:
			return
		doc = frappe.new_doc("Resto Waste")
		doc.update({"company": site.company, "posting_date": frappe.utils.nowdate(), "warehouse": site.kitchen, "items": rows})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()

	# ---- the main safe

	def safe_balance(self):
		from resto.resto import cash_guard

		return cash_guard.balance(self.site.safe, self.site.company, frappe.utils.nowdate())

	def pay_suppliers(self):
		"""Once a week the oldest supplier invoices left on credit are paid from the main safe, as far as it holds."""
		site = self.site
		budget = flt(self.safe_balance() * 0.6, 2)
		if budget <= 0:
			return
		due = {}
		for inv in frappe.get_all(
			"Purchase Invoice",
			filters={"company": site.company, "docstatus": 1, "is_return": 0, "outstanding_amount": (">", 0)},
			fields=["name", "supplier", "credit_to", "outstanding_amount"],
			order_by="posting_date asc, name asc",
		):
			if budget <= 0:
				break
			part = min(flt(inv.outstanding_amount), budget)
			budget -= part
			due.setdefault((inv.supplier, inv.credit_to), []).append((inv.name, flt(part, 2)))
		for (supplier, credit_to), invoices in due.items():
			self.supplier_payment(supplier, credit_to, invoices)

	def supplier_payment(self, supplier, credit_to, invoices):
		site = self.site
		amount = flt(sum(a for _n, a in invoices), 2)
		acc = lambda name: frappe.db.get_value("Account", name, ["account_currency", "account_type"], as_dict=True)  # noqa: E731
		frm, to = acc(site.safe), acc(credit_to)
		pe = frappe.new_doc("Payment Entry")
		pe.update(
			{
				"payment_type": "Pay",
				"company": site.company,
				"posting_date": frappe.utils.nowdate(),
				"party_type": "Supplier",
				"party": supplier,
				"party_account": credit_to,
				"paid_from": site.safe,
				"paid_from_account_currency": frm.account_currency,
				"paid_from_account_type": frm.account_type,
				"paid_to": credit_to,
				"paid_to_account_currency": to.account_currency,
				"paid_to_account_type": to.account_type,
				"paid_amount": amount,
				"received_amount": amount,
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": invoices[0][0],
				"reference_date": frappe.utils.nowdate(),
				"cost_center": site.cost_center,
				"remarks": _("Paid to the supplier from the main safe"),
			}
		)
		for name, part in invoices:
			pe.append("references", {"reference_doctype": "Purchase Invoice", "reference_name": name, "allocated_amount": part})
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
		self.count("supplier_payments")

	def salaries(self, day):
		"""The month's salaries at its end, from the main safe (Resto Salary Entry, with the workers' open advances taken off)."""
		from resto.resto.doctype.resto_salary_entry.resto_salary_entry import get_workers

		site = self.site
		month = get_first_day(day)
		if not site.settings.get("salary_account"):
			return self.warn_once("Resto Settings has no Salaries Account: no salaries")
		if frappe.db.exists("Resto Salary Entry", {"company": site.company, "salary_month": month, "docstatus": 1}):
			return
		workers = get_workers(site.company)
		if not workers:
			return self.warn_once("No active worker has a monthly salary: no salaries")
		net = sum(flt(w["net"]) for w in workers)
		if self.safe_balance() < net:
			return _log(f"  ! the main safe holds less than {getdate(day):%Y-%m}'s salaries ({flt(net, 2)}): tried again tomorrow")
		doc = frappe.new_doc("Resto Salary Entry")
		doc.update({"salary_month": month, "posting_date": frappe.utils.nowdate(), "company": site.company, "pay_from_account": site.safe})
		for w in workers:
			doc.append("workers", {k: w[k] for k in ("employee", "salary", "deduct", "deduct_amount")})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()

	def monthly_expense(self, m):
		"""A monthly expense given to the run (rent, electricity...): from the main safe to its account."""
		from resto.resto.shift import _set_remark

		site = self.site
		amount = flt(m.get("amount"), 2)
		if amount <= 0 or not m.get("account"):
			return
		remark = m.get("remarks") or m["account"]
		today = getdate(frappe.utils.nowdate())
		paid = frappe.db.sql(
			"""select 1 from `tabJournal Entry` je join `tabJournal Entry Account` a on a.parent = je.name
			where je.docstatus = 1 and je.company = %s and je.user_remark = %s and a.account = %s and a.debit > 0
			and je.posting_date between %s and %s limit 1""",
			(site.company, remark, m["account"], get_first_day(today), get_last_day(today)),
		)
		if paid:
			return
		if self.safe_balance() < amount:
			return _log(f"  ! the main safe holds less than {m['account']} ({amount}): tried again tomorrow")
		je = frappe.new_doc("Journal Entry")
		je.update({"voucher_type": "Cash Entry", "company": site.company, "posting_date": frappe.utils.nowdate()})
		_set_remark(je, remark)
		je.append("accounts", {"account": m["account"], "debit_in_account_currency": amount, "cost_center": site.cost_center})
		je.append("accounts", {"account": site.safe, "credit_in_account_currency": amount, "cost_center": site.cost_center})
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()


def _short_of(e):
	"""(item, qty short) from ERPNext's negative stock error, read from its item link or name (any language)."""
	if type(e).__name__ != "NegativeStockError":
		return None
	text = " ".join([str(e) or "", *(str(m) for m in (frappe.local.message_log or []))])
	# The item's link (its desk path differs between versions), else its name in the text.
	item_code, head = None, ""
	for found in re.finditer(r"/item/([^\"'<>\s?#]+)", text):
		code = unquote(found.group(1))
		if frappe.db.exists("Item", code):
			item_code, head = code, re.sub(r"<[^>]+>", " ", text[: found.start()])
			break
	if not item_code:
		plain = re.sub(r"<[^>]+>", " ", text)
		named = [
			(len(shown), code, plain.find(shown))
			for code, item_name in frappe.db.sql("select name, item_name from `tabItem` where is_stock_item = 1 and disabled = 0")
			for shown in {item_name, code}
			if shown and len(shown) > 2 and shown in plain
		]
		if not named:
			return None
		_n, item_code, at = max(named)
		head = plain[:at]
	number = re.search(r"(\d[\d,]*(?:\.\d+)?)", head)
	return item_code, flt(number.group(1).replace(",", "")) if number else 0


# --------------------------------------------------------------------------- the opening and the check


OPENING = (
	("Stock Reconciliation", {"purpose": "Opening Stock"}),
	("Stock Entry", {"is_opening": "Yes"}),
	("Journal Entry", {"is_opening": "Yes"}),
	("Journal Entry", {"voucher_type": "Opening Entry"}),
	("Sales Invoice", {"is_opening": "Yes"}),
	("Purchase Invoice", {"is_opening": "Yes"}),
)
LEDGERS = ("GL Entry", "Stock Ledger Entry", "Payment Ledger Entry")


def _openings(named=None):
	"""{(doctype, name): posting_date} of the submitted opening entries: those marked as opening
	(Opening Stock, Is Opening, Opening Entry), or the ones named, e.g.
	["Journal Entry:ACC-JV-2026-00001", "Stock Reconciliation:MAT-RECO-2026-00001"]."""
	out = {}
	if isinstance(named, str):
		named = json.loads(named) if named.strip().startswith("[") else [named]
	if named:
		for entry in named:
			doctype, name = entry.split(":", 1) if isinstance(entry, str) else entry
			doctype, name = doctype.strip(), name.strip()
			row = frappe.db.get_value(doctype, name, ["posting_date", "docstatus"], as_dict=True)
			if not row or row.docstatus != 1:
				frappe.throw(f"{doctype} {name} is not a submitted document")
			out[(doctype, name)] = getdate(row.posting_date)
		return out
	for doctype, filters in OPENING:
		for r in frappe.get_all(doctype, filters={**filters, "docstatus": 1}, fields=["name", "posting_date"]):
			out[(doctype, r.name)] = getdate(r.posting_date)
	return out


def _other_entries(openings):
	"""The documents on the ledgers that are not the opening's: {voucher type: how many}."""
	seen = set()
	for ledger in ("GL Entry", "Stock Ledger Entry"):
		for vt, vn in frappe.db.sql(f"select distinct voucher_type, voucher_no from `tab{ledger}`"):
			if (vt, vn) not in openings:
				seen.add((vt, vn))
	out = {}
	for vt, _vn in seen:
		out[vt] = out.get(vt, 0) + 1
	return out


def _kept(openings, keep=None):
	"""The documents kept with the opening: the assets' purchases and payments and their
	depreciation entries (ERPNext's asset schedules link them), and any named in keep."""
	out = set(openings)
	if frappe.db.table_exists("Journal Entry"):
		for name in frappe.get_all("Journal Entry", filters={"voucher_type": "Depreciation Entry"}, pluck="name"):
			out.add(("Journal Entry", name))
		for name in frappe.db.sql_list(
			"select distinct parent from `tabJournal Entry Account` where reference_type = 'Asset' and parenttype = 'Journal Entry'"
		):
			out.add(("Journal Entry", name))
	if frappe.db.table_exists("Depreciation Schedule") and frappe.db.has_column("Depreciation Schedule", "journal_entry"):
		for name in frappe.db.sql_list("select distinct journal_entry from `tabDepreciation Schedule` where ifnull(journal_entry, '') != ''"):
			out.add(("Journal Entry", name))
	invoices = set(
		frappe.db.sql_list(
			"""select distinct d.parent from `tabPurchase Invoice Item` d left join `tabItem` i on i.name = d.item_code
			where d.parenttype = 'Purchase Invoice' and (d.is_fixed_asset = 1 or i.is_fixed_asset = 1)"""
		)
	)
	if frappe.db.has_column("Asset", "purchase_invoice"):
		invoices |= set(frappe.db.sql_list("select distinct purchase_invoice from `tabAsset` where ifnull(purchase_invoice, '') != ''"))
	for name in invoices:
		out.add(("Purchase Invoice", name))
	if invoices:
		for name in frappe.db.sql_list(
			"""select distinct parent from `tabPayment Entry Reference`
			where parenttype = 'Payment Entry' and reference_doctype = 'Purchase Invoice' and reference_name in %s""",
			(tuple(invoices),),
		):
			out.add(("Payment Entry", name))
	if isinstance(keep, str):
		keep = json.loads(keep) if keep.strip().startswith("[") else [keep]
	for entry in keep or []:
		doctype, name = entry.split(":", 1) if isinstance(entry, str) else entry
		out.add((doctype.strip(), name.strip()))
	return out


def _late_prices(day):
	"""{"selling": n, "buying": n}: items whose price in a price list starts after day, with no price
	of that list valid on day (an order that day has no price: a site's prices are often dated the
	day they were typed in)."""
	rows = frappe.db.sql(
		"""select ip.selling, count(distinct ip.item_code, ip.price_list) from `tabItem Price` ip
		where ip.valid_from > %(day)s and ifnull(ip.customer, '') = '' and ifnull(ip.supplier, '') = ''
		and not exists (select 1 from `tabItem Price` o where o.item_code = ip.item_code and o.price_list = ip.price_list
			and ifnull(o.customer, '') = '' and ifnull(o.supplier, '') = ''
			and (o.valid_from is null or o.valid_from <= %(day)s) and (o.valid_upto is null or o.valid_upto >= %(day)s))
		group by ip.selling""",
		{"day": day},
	)
	out = {"selling": 0, "buying": 0}
	for selling, n in rows:
		out["selling" if cint(selling) else "buying"] += n
	return out


def _move_prices(day):
	"""On the demo copy: each such item's first price starts on day instead (its later prices kept)."""
	rows = frappe.db.sql(
		"""select ip.name from `tabItem Price` ip
		where ip.valid_from > %(day)s and ifnull(ip.customer, '') = '' and ifnull(ip.supplier, '') = ''
		and ip.valid_from = (select min(f.valid_from) from `tabItem Price` f where f.item_code = ip.item_code
			and f.price_list = ip.price_list and ifnull(f.customer, '') = '' and ifnull(f.supplier, '') = '')
		and not exists (select 1 from `tabItem Price` o where o.item_code = ip.item_code and o.price_list = ip.price_list
			and ifnull(o.customer, '') = '' and ifnull(o.supplier, '') = ''
			and (o.valid_from is null or o.valid_from <= %(day)s) and (o.valid_upto is null or o.valid_upto >= %(day)s))""",
		{"day": day},
	)
	names = [r[0] for r in rows]
	for part in _chunks(names):
		frappe.db.sql("update `tabItem Price` set valid_from = %s where name in %s", (day, tuple(part)))
	return len(names)


def _first_documents(limit=12):
	"""The earliest documents on the ledgers, to tell the opening by: (date, type, name, remark)."""
	rows = frappe.db.sql(
		"""select min(posting_date) as d, voucher_type, voucher_no from `tabGL Entry` where is_cancelled = 0
		group by voucher_type, voucher_no order by d, voucher_no limit %s""",
		limit,
	)
	out = []
	for d, vt, vn in rows:
		remark = None
		for field in ("remark", "user_remark", "remarks", "purpose"):
			if frappe.db.has_column(vt, field):
				remark = frappe.db.get_value(vt, vn, field)
				if remark:
					break
		out.append((str(d), vt, vn, re.sub(r"\s+", " ", str(remark or ""))[:80]))
	return out


def _fiscal_year(day, company):
	from erpnext.accounts.utils import get_fiscal_year

	try:
		return get_fiscal_year(day, company=company)[0]
	except Exception:
		return None


def _frozen_upto():
	for doctype, field in (("Accounts Settings", "acc_frozen_upto"), ("Stock Settings", "stock_frozen_upto")):
		try:
			value = frappe.db.get_single_value(doctype, field)
		except Exception:
			value = None
		if value and getdate(value) > getdate("1900-01-01"):  # an empty date reads 0001-01-01
			yield doctype, getdate(value)


def _redate_opening(day_before, openings):
	"""Move the opening entries back to day_before, every one by the same days (their order kept):
	the documents and their ledger rows, so the stock and the accounts stay exactly as they were."""
	if not openings:
		return 0
	delta = date_diff(max(openings.values()), day_before)
	if delta <= 0:
		return 0
	company = frappe.db.get_single_value("Resto Settings", "company")
	for (doctype, name), posting_date in openings.items():
		new_date = add_days(posting_date, -delta)
		fy = _fiscal_year(new_date, company)
		if not fy:
			frappe.throw(f"No fiscal year holds {new_date}: add it first")
		frappe.db.sql(f"update `tab{doctype}` set posting_date = %s where name = %s", (new_date, name))
		if frappe.db.has_column(doctype, "set_posting_time"):
			frappe.db.sql(f"update `tab{doctype}` set set_posting_time = 1 where name = %s", name)
		for field in ("due_date", "bill_date"):
			if frappe.db.has_column(doctype, field):
				frappe.db.sql(f"update `tab{doctype}` set {field} = date_sub({field}, interval %s day) where name = %s and {field} is not null", (delta, name))
		if frappe.db.table_exists("Payment Schedule"):
			frappe.db.sql(
				"update `tabPayment Schedule` set due_date = date_sub(due_date, interval %s day) where parenttype = %s and parent = %s",
				(delta, doctype, name),
			)
		for ledger in LEDGERS + ("Advance Payment Ledger Entry", "Serial and Batch Bundle"):
			if not frappe.db.table_exists(ledger):
				continue
			for field in ("posting_date", "posting_datetime", "transaction_date", "due_date"):
				if frappe.db.has_column(ledger, field):
					frappe.db.sql(
						f"update `tab{ledger}` set {field} = date_sub({field}, interval %s day) where voucher_type = %s and voucher_no = %s and {field} is not null",
						(delta, doctype, name),
					)
			if frappe.db.has_column(ledger, "fiscal_year"):
				frappe.db.sql(f"update `tab{ledger}` set fiscal_year = %s where voucher_type = %s and voucher_no = %s", (fy, doctype, name))
	return delta


@frappe.whitelist()
def check(openings=None, keep=None, start=None):
	"""What the site has for the demo, and what stops it. openings, keep: as for generate; start: the
	first day to be played (the prices must be valid on it)."""
	_guard()
	frappe.only_for("System Manager")
	from frappe.utils.scheduler import is_scheduler_disabled

	problems, notes = [], []
	try:
		site = _Site(seed=1)
	except Exception as e:
		problems.append(re.sub(r"<[^>]+>", "", str(e)))
		site = None
	if site:
		s = site.settings
		notes.append(
			f"{len(site.menu)} dishes, {len(site.bundles)} with a recipe bundle, {len(site.prepared)} prepared items, "
			f"{len(site.tables)} tables, {len(site.customers)} customers with a phone, {len(site.employees)} workers "
			f"({len(site.drivers)} drivers), {len(site.suppliers)} suppliers"
		)
		notes.append(f"cashiers {site.cashiers}, waiters {site.waiters}, kitchen {site.cooks}, approving manager {site.manager}")
		notes.append(f"payment methods {site.modes} (cash: {site.cash_modes})")
		for field, what in (
			("waste_account", "waste"),
			("staff_meal_account", "staff meals"),
			("salary_account", "salaries"),
		):
			if not s.get(field):
				notes.append(f"no {field} in Resto Settings: no {what}")
		parts = {k for v in site.bundles.values() for k, _q in v}
		parts |= {k for p in site.prepared.values() for k in p.per}
		no_cost = [
			i for i in parts if i not in site.prepared and cint(frappe.get_cached_value("Item", i, "is_stock_item")) and not site.rate(i)
		]
		if no_cost:
			notes.append(f"{len(no_cost)} recipe items have no cost known and are not bought: {sorted(no_cost)[:10]}")
		if not site.cash_modes:
			problems.append("the POS Profile has no cash payment method")
	openings = _openings(openings)
	for (doctype, name), day in sorted(openings.items(), key=lambda x: x[1]):
		notes.append(f"opening: {day} {doctype} {name}")
	if not openings:
		notes.append("no opening entry found: name them with openings=[\"Doctype:name\", ...]")
	for row in _first_documents():
		notes.append("earliest on the books: " + " | ".join(row))
	kept = _kept(openings, keep)
	extra = {}
	for doctype, _name in kept - set(openings):
		extra[doctype] = extra.get(doctype, 0) + 1
	if extra:
		notes.append(f"kept with the opening (assets, depreciation, named): {extra}")
	other = _other_entries(kept)
	if start:
		late = _late_prices(getdate(start))
		if late["selling"]:
			problems.append(
				f"selling prices of {late['selling']} items start after {start}: generate with move_prices=1 moves them to {start} (this copy only)"
			)
		if late["buying"]:
			notes.append(f"buying prices of {late['buying']} items start after {start} (moved too with move_prices=1)")
	else:
		first = frappe.db.sql("select min(valid_from) from `tabItem Price` where selling = 1")[0][0]
		if first:
			notes.append(f"the earliest selling price is valid from {first}: pass start=... to check it")
	done = frappe.db.get_global(MARK)
	if other and not done:
		problems.append(f"documents other than the opening are on the books (reset first): {other}")
	if done:
		notes.append(f"demo done up to {done} with {frappe.db.get_global(ARGS)}")
	for doctype, value in _frozen_upto():
		problems.append(f"{doctype} is frozen up to {value}")
	if frappe.db.exists("Period Closing Voucher", {"docstatus": 1}):
		problems.append("a Period Closing Voucher is submitted")
	if not is_scheduler_disabled(verbose=False):
		problems.append("the scheduler is on: bench --site <site> disable-scheduler")
	for line in notes:
		_log(f"- {line}")
	for line in problems:
		_log(f"X {line}")
	_log("ready" if not problems else "not ready")
	return {"ready": not problems, "problems": problems, "notes": notes}


# --------------------------------------------------------------------------- reset


TRANSACTIONS = (
	"Resto Order",
	"Resto Kitchen Ticket",
	"Resto Shift",
	"Resto Prep",
	"Resto Waste",
	"Resto Salary Entry",
	"Resto Approval Log",
	"Resto Reservation",
	"Sales Invoice",
	"POS Invoice",
	"POS Opening Entry",
	"POS Closing Entry",
	"Purchase Invoice",
	"Purchase Receipt",
	"Purchase Order",
	"Delivery Note",
	"Sales Order",
	"Material Request",
	"Payment Entry",
	"Payment Request",
	"Journal Entry",
	"Stock Entry",
	"Stock Reconciliation",
	"Landed Cost Voucher",
	"Repost Item Valuation",
	"Repost Payment Ledger",
	"Unreconcile Payment",
)


def _chunks(names, size=500):
	names = list(names)
	for n in range(0, len(names), size):
		yield names[n : n + size]


@frappe.whitelist()
def reset(confirm=None, openings=None, keep=None, dry_run=0):
	"""Delete every transaction but the opening entries (orders, shifts, invoices, payments, journal
	and stock entries), with their ledger rows, and set the stock back to the opening's: the copy
	starts clean for generate(). The assets' purchases, payments and depreciation entries stay with
	the opening, and so do the documents named in keep (["Payment Entry:ACC-PAY-...", ...]).
	dry_run: list what would go, delete nothing. confirm: the site's name; openings: as for generate."""
	_guard()
	frappe.only_for("System Manager")
	if not cint(dry_run) and confirm != frappe.local.site:
		frappe.throw(f"Pass confirm={frappe.local.site!r} to delete this site's transactions")
	if frappe.db.table_exists("Serial and Batch Bundle") and frappe.db.count("Serial and Batch Bundle"):
		frappe.throw("Serial and batch numbers are used here: the reset does not handle them")
	openings = _openings(openings)
	if not openings:
		frappe.throw('No opening entry found: name them with openings=["Doctype:name", ...]')
	kept = _kept(openings, keep)
	if cint(dry_run):
		return _would_delete(kept)
	moved = [k for k in kept - set(openings) if frappe.db.exists("Stock Ledger Entry", {"voucher_type": k[0], "voucher_no": k[1]})]
	if moved:
		frappe.throw(f"Documents kept besides the opening move stock, which the reset does not handle: {moved[:10]}")
	# An opening entry dated after other stock moves of its item has values that count them.
	last_opening = {}
	for r in frappe.db.sql(
		"""select voucher_type, voucher_no, item_code, warehouse, timestamp(posting_date, posting_time) as at
		from `tabStock Ledger Entry` where is_cancelled = 0""",
		as_dict=True,
	):
		key = (r.item_code, r.warehouse)
		if (r.voucher_type, r.voucher_no) in openings:
			last_opening[key] = max(last_opening.get(key, r.at), r.at)
	tainted = frappe.db.sql(
		"""select voucher_type, voucher_no, item_code, warehouse, timestamp(posting_date, posting_time) as at
		from `tabStock Ledger Entry` where is_cancelled = 0""",
		as_dict=True,
	)
	early = {
		(r.voucher_type, r.voucher_no)
		for r in tainted
		if (r.voucher_type, r.voucher_no) not in openings and (r.item_code, r.warehouse) in last_opening and r.at < last_opening[(r.item_code, r.warehouse)]
	}
	if early:
		frappe.throw(f"These stock entries come before the opening of the same items, so its values count them: {sorted(early)[:10]}")

	deleted = {}
	for doctype in TRANSACTIONS:
		if not frappe.db.table_exists(doctype):
			continue
		names = [n for n in frappe.db.sql_list(f"select name from `tab{doctype}`") if (doctype, n) not in kept]
		if not names:
			continue
		tables = [df.options for df in frappe.get_meta(doctype).get_table_fields()]
		for part in _chunks(names):
			part = tuple(part)
			for child in tables:
				frappe.db.sql(f"delete from `tab{child}` where parenttype = %s and parent in %s", (doctype, part))
			frappe.db.sql(f"delete from `tab{doctype}` where name in %s", (part,))
			frappe.db.sql("delete from `tabVersion` where ref_doctype = %s and docname in %s", (doctype, part))
			frappe.db.sql("delete from `tabComment` where reference_doctype = %s and reference_name in %s", (doctype, part))
		deleted[doctype] = len(names)
	for ledger in LEDGERS + ("Advance Payment Ledger Entry",):
		if not frappe.db.table_exists(ledger):
			continue
		rows = frappe.db.sql(f"select name, voucher_type, voucher_no from `tab{ledger}`")
		gone = [n for n, vt, vn in rows if (vt, vn) not in kept]
		for part in _chunks(gone):
			frappe.db.sql(f"delete from `tab{ledger}` where name in %s", (tuple(part),))
		deleted[ledger] = len(gone)

	# The stock: what the last opening row of each item and warehouse left.
	for b in frappe.get_all("Bin", fields=["name", "item_code", "warehouse"]):
		last = frappe.db.sql(
			"""select qty_after_transaction, valuation_rate, stock_value from `tabStock Ledger Entry`
			where item_code = %s and warehouse = %s and is_cancelled = 0
			order by posting_date desc, posting_time desc, creation desc limit 1""",
			(b.item_code, b.warehouse),
		)
		qty, rate, value = last[0] if last else (0, 0, 0)
		values = {"actual_qty": qty, "valuation_rate": rate, "stock_value": value, "projected_qty": qty}
		for field in (
			"ordered_qty",
			"reserved_qty",
			"indented_qty",
			"planned_qty",
			"reserved_qty_for_production",
			"reserved_qty_for_sub_contract",
			"reserved_qty_for_production_plan",
			"reserved_stock",
		):
			if frappe.db.has_column("Bin", field):
				values[field] = 0
		frappe.db.set_value("Bin", b.name, values, update_modified=False)
	# Opening invoices: what is still owed on them, from what is left of the payment ledger.
	for (doctype, name) in openings:
		if doctype in ("Sales Invoice", "Purchase Invoice"):
			owed = frappe.db.sql(
				"""select sum(amount_in_account_currency) from `tabPayment Ledger Entry`
				where against_voucher_type = %s and against_voucher_no = %s and delinked = 0""",
				(doctype, name),
			)[0][0]
			frappe.db.set_value(doctype, name, "outstanding_amount", abs(flt(owed)), update_modified=False)
	_forget()
	frappe.db.commit()  # nosemgrep: a whole reset or none
	frappe.clear_cache()
	_log(f"deleted: {deleted}")
	_log(f"kept {len(openings)} opening entries and {len(kept) - len(openings)} documents with them")
	return deleted


def _would_delete(kept):
	"""What a reset would delete, by document type: name, date, status and remark of each."""
	out = {}
	for doctype in TRANSACTIONS:
		if not frappe.db.table_exists(doctype):
			continue
		meta = frappe.get_meta(doctype)
		date = next((f for f in ("posting_date", "transaction_date", "opened_at", "opening_time", "creation") if f == "creation" or meta.has_field(f)), "creation")
		remark = next((f for f in ("remark", "user_remark", "remarks", "purpose", "supplier", "customer") if meta.has_field(f)), None)
		fields = ["name", "docstatus", f"{date} as on_date"] + ([f"{remark} as remark"] if remark else [])
		rows = [r for r in frappe.get_all(doctype, fields=fields, order_by=f"{date} asc") if (doctype, r.name) not in kept]
		if not rows:
			continue
		out[doctype] = len(rows)
		_log(f"{doctype}: {len(rows)}")
		for r in rows[:60]:
			text = re.sub(r"\s+", " ", str(r.get("remark") or ""))[:70]
			_log(f"  {r.on_date} {r.name} {('draft', 'submitted', 'cancelled')[r.docstatus]} {text}")
		if len(rows) > 60:
			_log(f"  ... and {len(rows) - 60} more")
	kept_extra = sorted(k for k in kept if k[0] in TRANSACTIONS)
	_log(f"kept ({len(kept_extra)}): " + ", ".join(f"{d} {n}" for d, n in kept_extra[:80]))
	return out


# --------------------------------------------------------------------------- generate


def _forget():
	frappe.db.sql("delete from `tabDefaultValue` where parent = '__global' and defkey in (%s, %s)", (MARK, ARGS))
	frappe.clear_cache()


@frappe.whitelist()
def generate(
	start, end, orders_per_day=30, seed=1, monthly=None, change_float=None, force_scheduler=0, opening=1, openings=None, keep=None, move_prices=0
):
	"""Play the restaurant's days from start to end (see the module's notes).

	monthly: expenses paid from the main safe each month, e.g.
	[{"account": "Rent - X", "amount": 15000, "day": 1, "remarks": "Rent"}].
	change_float: cash kept in the drawer after each cash drop (default: about five dishes' worth).
	opening: 1 (the copy of a site): only the opening on the books, moved to the day before start;
	0: the books are kept, and nothing may be dated on or after start (the tests' site).
	openings: the opening entries when they are not marked as such, ["Journal Entry:ACC-JV-...", ...].
	keep: other documents left on the books, as for reset (the assets' are kept anyway).
	move_prices: 1 moves prices that start after the first day back to it (this copy's Item Prices)."""
	_guard()
	frappe.only_for("System Manager")
	from frappe.utils.scheduler import is_scheduler_disabled

	start, end = getdate(start), getdate(end)
	if end < start:
		frappe.throw("end is before start")
	if isinstance(monthly, str):
		monthly = json.loads(monthly)
	if not frappe.flags.in_test and not cint(force_scheduler) and not is_scheduler_disabled(verbose=False):
		frappe.throw("Turn the scheduler off first: bench --site <site> disable-scheduler")
	# The days' plans hang on these; the end may move (a short run first, then on to a later end).
	args = {"start": str(start), "orders_per_day": cint(orders_per_day), "seed": cint(seed)}
	done = frappe.db.get_global(MARK)
	if done:
		saved = json.loads(frappe.db.get_global(ARGS) or "{}")
		if saved and saved != args:
			frappe.throw(f"This site's demo was started with {saved}: run it with the same, or reset to start over")
	else:
		if cint(opening):
			_first_day_ready(start, openings, keep)
		else:
			for ledger in ("GL Entry", "Stock Ledger Entry"):
				if frappe.db.sql(f"select 1 from `tab{ledger}` where is_cancelled = 0 and posting_date >= %s limit 1", start):
					frappe.throw(f"{ledger} rows are dated on or after {start}: start later")
		frappe.db.set_global(ARGS, json.dumps(args))
		_commit()
	first = add_days(getdate(done), 1) if done else start
	if first > end:
		_log(f"done already up to {done}")
		return done
	for day in (add_days(first, -1), end):
		if not _fiscal_year(day, frappe.db.get_single_value("Resto Settings", "company")):
			frappe.throw(f"No fiscal year holds {day}: add it first")
	late = _late_prices(first)
	if late["selling"] or late["buying"]:
		if not cint(move_prices):
			frappe.throw(
				f"Prices start after {first} (selling: {late['selling']} items, buying: {late['buying']}): "
				"run with move_prices=1 to move them to that day on this copy"
			)
		_log(f"{_move_prices(first)} prices moved to start on {first}")
		_commit()

	user = frappe.session.user
	site = _Site(cint(seed))
	planner = _Planner(site, start, end, cint(orders_per_day), cint(seed))
	clock = _Clock(_at(first, "06:00"))
	pin = _demo_pin(site.manager)
	_commit()
	last = done
	try:
		with _patched(clock):
			run = _Run(site, planner, clock, change_float, monthly)
			day = first
			while day <= end:
				run.day(day)
				frappe.db.set_global(MARK, str(day))
				_commit()
				last = str(day)
				day = add_days(day, 1)
	except BaseException:
		frappe.db.rollback()
		_log(f"stopped; done up to {last}: run it again with the same arguments to go on")
		raise
	finally:
		frappe.set_user("Administrator")
		_restore_pin(*pin)
		_commit()
		frappe.set_user(user)
	_log(f"done up to {last}")
	return last


def _first_day_ready(start, named=None, keep=None):
	"""Before the first day: nothing on the books but the opening (and the assets' entries), which
	moves to the day before."""
	openings = _openings(named)
	other = _other_entries(_kept(openings, keep))
	if other:
		frappe.throw(f"Documents other than the opening are on the books, run reset first: {other}")
	for doctype, value in _frozen_upto():
		if value >= add_days(start, -1):
			frappe.throw(f"{doctype} is frozen up to {value}")
	if frappe.db.exists("Period Closing Voucher", {"docstatus": 1}):
		frappe.throw("A Period Closing Voucher is submitted")
	delta = _redate_opening(add_days(start, -1), openings)
	if delta:
		_log(f"opening entries moved back {delta} days, to {add_days(start, -1)} at the latest")
