"""Setup check: lists what is missing or risky before the restaurant starts selling.

Each result is {"level": "error" | "warning" | "ok", "area": ..., "message": ...}.
Errors stop correct billing or stock; warnings are worth a look.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.utils import CASHIER, KITCHEN, MANAGER, WAITER, get_settings, posnext_installed, require_role


@frappe.whitelist()
def run():
	require_role(MANAGER)
	return SetupCheck().run()


class SetupCheck:
	def __init__(self):
		self.s = get_settings()
		self.results = []

	def add(self, level, area, message):
		self.results.append({"level": level, "area": area, "message": message})

	def error(self, area, message):
		self.add("error", area, message)

	def warn(self, area, message):
		self.add("warning", area, message)

	def ok(self, area, message):
		self.add("ok", area, message)

	def run(self):
		for check in (
			self.check_basics,
			self.check_pos_profile,
			self.check_service,
			self.check_floor,
			self.check_kitchen,
			self.check_receipt_format,
			self.check_users,
			self.check_menu,
			self.check_prepared,
			self.check_kitchen_stock,
			self.check_recipe_precision,
			self.check_modifiers,
			self.check_sides,
			self.check_stale_bundles,
		):
			check()
		order = {"error": 0, "warning": 1, "ok": 2}
		self.results.sort(key=lambda r: order[r["level"]])
		return self.results

	# Settings and masters.
	def check_basics(self):
		s = self.s
		area = _("Settings")
		if not s.company:
			self.error(area, _("Set the company in Resto Settings"))
		for field, label in (
			("selling_price_list", _("Selling Price List")),
			("main_warehouse", _("Main Store")),
			("kitchen_warehouse", _("Kitchen Warehouse")),
			("menu_item_group", _("Menu Item Group")),
			("prepared_item_group", _("Prepared Item Group")),
			("waste_account", _("Waste Account")),
		):
			if not s.get(field):
				self.error(area, _("{0} is not set in Resto Settings").format(label))
		if s.selling_price_list and not frappe.db.get_value("Price List", s.selling_price_list, "enabled"):
			self.error(area, _("Price list {0} is disabled").format(s.selling_price_list))
		if flt(s.delivery_fee) and not s.delivery_fee_item:
			self.error(area, _("A delivery fee is set but the Delivery Fee Item is not"))
		if s.delivery_fee_item and frappe.db.get_value("Item", s.delivery_fee_item, "is_stock_item"):
			self.error(area, _("Delivery Fee Item {0} must be a non-stock item").format(s.delivery_fee_item))

	def check_pos_profile(self):
		s = self.s
		area = _("POS Profile")
		if not s.pos_profile:
			self.error(area, _("Set the POS Profile in Resto Settings; bills cannot be settled without it"))
			return
		p = frappe.get_doc("POS Profile", s.pos_profile)
		if p.disabled:
			self.error(area, _("POS Profile {0} is disabled").format(p.name))
		if s.company and p.company != s.company:
			self.error(area, _("POS Profile {0} does not belong to company {1}").format(p.name, s.company))
		if not p.update_stock:
			self.error(area, _("POS Profile {0} does not update stock; recipe ingredients would never be deducted").format(p.name))
		if not p.customer:
			self.error(area, _("Set a default customer in POS Profile {0}").format(p.name))
		if not p.payments:
			self.error(area, _("POS Profile {0} has no payment methods").format(p.name))
		for row in p.payments:
			account = frappe.db.get_value(
				"Mode of Payment Account", {"parent": row.mode_of_payment, "company": p.company}, "default_account"
			)
			if not account:
				self.error(area, _("Payment method {0} has no account for company {1}").format(row.mode_of_payment, p.company))
			elif (
				p.get("account_for_change_amount")
				and frappe.db.get_value("Mode of Payment", row.mode_of_payment, "type") == "Cash"
				and account != p.account_for_change_amount
			):
				# Change is credited to this account while the full cash tendered is debited to the payment account.
				self.error(
					area,
					_("Cash account {0} of {1} differs from the change account {2}; the drawer would be overstated by the change given").format(
						account, row.mode_of_payment, p.account_for_change_amount
					),
				)
		if s.kitchen_warehouse and p.warehouse != s.kitchen_warehouse:
			self.warn(
				area,
				_("POS Profile warehouse is {0} but the kitchen warehouse is {1}; sales will take stock from {0}").format(
					p.warehouse, s.kitchen_warehouse
				),
			)
		if s.selling_price_list and p.selling_price_list and p.selling_price_list != s.selling_price_list:
			self.warn(
				area,
				_("POS Profile price list {0} differs from Resto's {1}; POSNext and the floor would sell at different prices").format(
					p.selling_price_list, s.selling_price_list
				),
			)
		if not p.write_off_account or not p.write_off_cost_center:
			self.warn(area, _("Set the write off account and cost center in POS Profile {0} (used for rounding)").format(p.name))
		from resto.resto.billing import taxes_inclusive_setting

		template_inclusive = bool(
			p.taxes_and_charges
			and frappe.db.exists(
				"Sales Taxes and Charges",
				{"parent": p.taxes_and_charges, "parenttype": "Sales Taxes and Charges Template", "included_in_print_rate": 1},
			)
		)
		posnext_inclusive = taxes_inclusive_setting(p.name)
		inclusive = template_inclusive if posnext_inclusive is None else posnext_inclusive
		if not p.taxes_and_charges:
			self.warn(area, _("POS Profile {0} has no tax template; bills will have no VAT").format(p.name))
		if posnext_inclusive is not None and template_inclusive != posnext_inclusive:
			self.warn(
				area,
				_("Tax template {0} and POSNext POS Settings disagree on tax inclusive prices; POSNext decides, so floor bills treat prices as {1}").format(
					p.taxes_and_charges, _("including tax") if posnext_inclusive else _("excluding tax")
				),
			)
		if inclusive and flt(s.service_charge_percent):
			self.error(
				area,
				_("Prices include tax for POS Profile {0} and a service charge is set; such bills are refused. Turn off one of them.").format(p.name),
			)
		elif inclusive:
			self.ok(area, _("Menu prices include tax"))
		if posnext_installed():
			if not s.require_pos_shift:
				self.warn(area, _("Bills can be settled without a POSNext shift; the shift closing will not count them"))
		if not self.results or all(r["area"] != area or r["level"] == "ok" for r in self.results):
			self.ok(area, _("POS Profile {0} is ready").format(p.name))

	def check_service(self):
		s = self.s
		area = _("Service Charge")
		if not flt(s.service_charge_percent):
			self.ok(area, _("No service charge"))
			return
		if not s.service_charge_account:
			self.error(area, _("Set the Service Charge Account in Resto Settings first"))
			return
		company, is_group = frappe.db.get_value("Account", s.service_charge_account, ["company", "is_group"]) or (None, None)
		if company != s.company or is_group:
			self.error(area, _("Choose a non-group account of company {0}").format(s.company))
		template = s.pos_profile and frappe.db.get_value("POS Profile", s.pos_profile, "taxes_and_charges")
		in_template = template and frappe.db.exists(
			"Sales Taxes and Charges",
			{"parent": template, "parenttype": "Sales Taxes and Charges Template", "account_head": s.service_charge_account},
		)
		if in_template:
			self.ok(area, _("Tax template {0} has a service row; VAT on service follows the template").format(template))
		elif cint(s.get("vat_on_service")):
			self.ok(area, _("VAT is charged on items plus service"))
		else:
			self.warn(area, _("VAT is charged on the items only, not on the service charge"))
		self.ok(area, _("Service charge {0}% to {1}").format(flt(s.service_charge_percent), s.service_charge_account))

	def check_floor(self):
		area = _("Floor", context="Resto")
		tables = frappe.db.count("Resto Table", {"is_active": 1})
		if not tables:
			self.warn(area, _("No active tables; only takeaway and delivery can be used"))
		else:
			self.ok(area, _("{0} active tables").format(tables))
		no_seats = frappe.db.get_all("Resto Table", {"is_active": 1, "seats": 0}, pluck="name")
		if no_seats:
			self.warn(area, _("Tables without seats (table reports need them): {0}").format(", ".join(no_seats)))

	def check_kitchen(self):
		area = _("Kitchen")
		stations = frappe.db.get_all(
			"Resto Kitchen Station", {"is_active": 1}, ["name", "is_default", "printer", "auto_print", "print_method"]
		)
		if not stations:
			self.error(area, _("No kitchen station; orders cannot be sent to the kitchen"))
			return
		defaults = [st.name for st in stations if st.is_default]
		if not defaults:
			self.warn(area, _("No default station; items that match no station cannot be sent"))
		for st in stations:
			if st.print_method == "Server Printer" and st.auto_print and not st.printer:
				self.warn(area, _("Station {0} prints automatically but has no printer").format(st.name))
		from resto.resto.kitchen import print_alerts

		alerts = print_alerts()
		for name in alerts["no_screen"]:
			self.warn(area, _("No kitchen screen prints automatically for station {0} now").format(name))
		if alerts["unprinted"]:
			self.warn(area, _("{0} kitchen tickets are not printed").format(len(alerts["unprinted"])))
		self.ok(area, _("{0} kitchen stations").format(len(stations)))

	def check_receipt_format(self):
		from resto.resto import printing

		area = _("Printing")

		s = self.s
		fmt = s.get("receipt_print_format")
		labelled = [r for r in (s.get("receipt_copies") or []) if r.label or cint(r.get("show_sides"))]
		if labelled and fmt != printing.RECEIPT_FORMAT:
			self.warn(
				area,
				_("Receipt copies have labels or sides, but only the {0} print format prints them").format(
					printing.RECEIPT_FORMAT
				),
			)

	def check_users(self):
		area = _("Users")
		for role in (WAITER, CASHIER, KITCHEN):
			users = frappe.db.sql(
				"""select count(distinct r.parent) from `tabHas Role` r join `tabUser` u on u.name = r.parent
				where r.role = %s and r.parenttype = 'User' and u.enabled = 1""",
				role,
			)[0][0]
			if not users:
				self.warn(area, _("No user has the role {0}").format(_(role)))

	# Menu: price, recipe bundle.
	def menu_items(self):
		group = self.s.menu_item_group
		if not group:
			return []
		lft, rgt = frappe.db.get_value("Item Group", group, ["lft", "rgt"])
		return frappe.db.sql(
			"""select i.name, i.is_stock_item from `tabItem` i join `tabItem Group` g on g.name = i.item_group
			where g.lft >= %s and g.rgt <= %s and i.disabled = 0 and i.is_sales_item = 1""",
			(lft, rgt),
			as_dict=True,
		)

	def check_menu(self):
		s = self.s
		area = _("Menu", context="Resto")
		items = self.menu_items()
		if not items:
			self.warn(area, _("No sales items in the menu item group"))
			return
		names = [i.name for i in items]
		priced = set(
			frappe.db.get_all("Item Price", {"price_list": s.selling_price_list, "item_code": ["in", names]}, pluck="item_code")
		) if s.selling_price_list else set()
		bundles = set(frappe.db.get_all("Product Bundle", {"new_item_code": ["in", names], "disabled": 0}, pluck="new_item_code"))
		no_price = [i.name for i in items if i.name not in priced]
		no_recipe = [i.name for i in items if not i.is_stock_item and i.name not in bundles]
		if no_price:
			self.warn(area, _("Menu items without a price (hidden on the floor): {0}").format(short(no_price)))
		if no_recipe:
			self.warn(area, _("Non-stock menu items without a recipe bundle (no stock or cost is deducted): {0}").format(short(no_recipe)))
		self.ok(area, _("{0} menu items").format(len(items)))

	def check_prepared(self):
		area = _("Recipes")
		recipes = frappe.db.get_all("Resto Recipe", {"recipe_type": "Prepared Ingredient", "is_active": 1}, ["name", "item", "bom"])
		no_bom = [r.name for r in recipes if not r.bom or frappe.db.get_value("BOM", r.bom, "docstatus") != 1]
		if no_bom:
			self.error(area, _("Prepared recipes without a submitted BOM (daily prep cannot post them): {0}").format(short(no_bom)))
		missing_cost = frappe.db.get_all("Resto Recipe", {"is_active": 1, "total_cost": 0}, pluck="name")
		if missing_cost:
			self.warn(area, _("Recipes with zero cost (update recipe costs or check purchase prices): {0}").format(short(missing_cost)))
		# A prepared item sold before its first prep needs a rate, or every sale of its dishes fails.
		no_rate = [
			r.item
			for r in recipes
			if not frappe.db.exists("Stock Ledger Entry", {"item_code": r.item, "is_cancelled": 0})
			and not flt(frappe.db.get_value("Item", r.item, "valuation_rate"))
		]
		if no_rate:
			self.error(
				area,
				_("Prepared items with no stock history and no valuation rate (sales would fail until a daily prep is posted): {0}").format(short(no_rate)),
			)

	def check_recipe_precision(self):
		"""The stock ledger keeps quantities to the system float precision; finer recipe quantities are rounded on every sale."""
		area = _("Recipes")
		precision = cint(frappe.db.get_single_value("System Settings", "float_precision")) or 3
		rows = frappe.db.sql(
			"""select pb.new_item_code, pbi.item_code, pbi.qty from `tabProduct Bundle Item` pbi
			join `tabProduct Bundle` pb on pb.name = pbi.parent where pb.disabled = 0""",
			as_dict=True,
		)
		rounded = sorted({f"{r.new_item_code} ({r.item_code} {flt(r.qty, 6)})" for r in rows if abs(flt(r.qty, precision) - flt(r.qty, 9)) > 1e-9})
		if rounded:
			self.warn(
				area,
				_("Recipe quantities finer than {0} decimals are rounded by the stock ledger on every sale; raise Float Precision in System Settings or use a smaller unit (gram, ml): {1}").format(
					precision, short(rounded)
				),
			)

	def check_modifiers(self):
		area = _("Modifier Group")
		rows = frappe.db.sql(
			"""select distinct o.item, i.is_stock_item, i.stock_uom, u.must_be_whole_number
			from `tabResto Modifier Option` o join `tabItem` i on i.name = o.item
			left join `tabUOM` u on u.name = i.stock_uom
			where ifnull(o.item, '') != ''""",
			as_dict=True,
		)
		bundles = set(frappe.db.get_all("Product Bundle", {"disabled": 0}, pluck="new_item_code"))
		no_recipe = [r.item for r in rows if not r.is_stock_item and r.item not in bundles]
		loose = [f"{r.item} (1 {r.stock_uom})" for r in rows if r.is_stock_item and not r.must_be_whole_number]
		if no_recipe:
			self.warn(area, _("Paid options billed as non-stock items without a recipe bundle (no stock or cost is deducted): {0}").format(short(no_recipe)))
		if loose:
			self.warn(area, _("Paid options billed as stock items: each option deducts one whole stock unit; give them a recipe instead: {0}").format(short(loose)))

	def check_sides(self):
		"""Changed sides are packed on the dish's invoice row (resto/resto/sides.py)."""
		area = _("Sides")
		# Not needed by sides (they are packed on the dish, never billed at 0), but a 0-rate stock
		# line anywhere on a floor bill would fail with it.
		if cint(frappe.db.get_single_value("Selling Settings", "validate_selling_price")):
			self.warn(
				_("POS Profile"),
				_("Selling Settings stops a sale below the valuation rate: a free or heavily discounted stock item blocks the bill at the cashier."),
			)
		if not cint(self.s.get("allow_side_changes")):
			if cint(self.s.get("charge_extra_sides")):
				self.warn(area, _("Extra portions are charged only when staff may change the sides served; that setting is off."))
			return
		from resto.resto import sides

		sides.clear_cache()
		charge = cint(self.s.get("charge_extra_sides"))
		priced_groups = set(frappe.get_all("Resto Side Option", filters={"extra_price": (">", 0)}, pluck="parent")) if charge else set()
		odd, free_portion = [], []
		for recipe in frappe.get_all("Resto Recipe", {"is_active": 1, "recipe_type": "Menu Dish"}, pluck="item"):
			dish_slots = sides.slots(recipe)
			if not dish_slots:
				continue
			try:
				sides.dish_components(recipe, {})
			except frappe.ValidationError:
				frappe.clear_last_message()
				odd.append(recipe)
			for slot in dish_slots.values():
				if slot.group in priced_groups and not sides._priced_slot_ok(slot):
					free_portion.append(f"{recipe}: {slot.label}")
		if odd:
			self.error(area, _("Dishes whose recipe bundle differs from the recipe; their sides cannot be changed until the recipe is saved again: {0}").format(short(odd)))
		if free_portion:
			self.warn(
				area,
				_("Dishes whose recipe serves a side in another quantity than one portion of its side group, so their extra portions are not charged: {0}").format(short(free_portion)),
			)
		if charge:
			bad_billing = []
			for o in frappe.get_all("Resto Side Option", filters={"extra_price": (">", 0)}, fields=["parent", "item", "portion_qty", "billing_item"]):
				if not o.billing_item or sides.extra_billing_error(o.item, o.portion_qty, o.billing_item):
					bad_billing.append(f"{o.parent}: {o.item}")
			if bad_billing:
				self.error(
					area,
					_("Priced side options whose billing item cannot be sold or does not deduct exactly one portion, so their extra portions are not charged: {0}").format(short(bad_billing)),
				)
		serial = frappe.db.sql(
			"""select distinct o.item from `tabResto Side Option` o join `tabItem` i on i.name = o.item
			where i.has_serial_no = 1 or i.has_batch_no = 1""",
			pluck=True,
		)
		if serial:
			self.warn(area, _("Side options with serial or batch numbers cannot be served as changed sides: {0}").format(short(serial)))
		precision = frappe.get_precision("Packed Item", "qty") or 3
		odd_portion = []
		for o in frappe.db.sql(
			"""select o.parent, o.item, o.portion_qty, ifnull(u.must_be_whole_number, 0) as whole
			from `tabResto Side Option` o join `tabItem` i on i.name = o.item left join `tabUOM` u on u.name = i.stock_uom""",
			as_dict=True,
		):
			q = flt(o.portion_qty)
			if (o.whole and abs(q - round(q)) > 1e-9) or abs(q - flt(q, precision)) > 1e-9:
				odd_portion.append(f"{o.parent}: {o.item} ({q:g})")
		if odd_portion:
			self.warn(area, _("Side options whose portion cannot be deducted exactly (a fraction of a whole unit, or more decimals than stock quantities keep): {0}").format(short(odd_portion)))

	def check_stale_bundles(self):
		"""A bundle whose recipe was deactivated or deleted still deducts stock on every sale."""
		area = _("Recipes")
		active = set(frappe.db.get_all("Resto Recipe", {"is_active": 1, "recipe_type": "Menu Dish"}, pluck="product_bundle"))
		managed = set(frappe.db.get_all("Resto Recipe", {"recipe_type": "Menu Dish"}, pluck="product_bundle"))
		stale = [b for b in managed if b and b not in active and frappe.db.get_value("Product Bundle", b, "disabled") == 0]
		if stale:
			self.warn(area, _("Inactive recipes whose bundle still deducts stock on every sale: {0}").format(short(stale)))

	def check_kitchen_stock(self):
		"""Dishes whose raw (non-prepared) components are short in the kitchen; settling them would fail."""
		s = self.s
		area = _("Kitchen Stock")
		if not s.kitchen_warehouse:
			return
		prepared_items = set()
		if s.prepared_item_group:
			lft, rgt = frappe.db.get_value("Item Group", s.prepared_item_group, ["lft", "rgt"])
			prepared_items = set(
				frappe.db.sql_list(
					"""select i.name from `tabItem` i join `tabItem Group` g on g.name = i.item_group
					where g.lft >= %s and g.rgt <= %s""",
					(lft, rgt),
				)
			)
		rows = frappe.db.sql(
			"""select pb.new_item_code dish, pbi.item_code, ifnull(b.actual_qty, 0) qty
			from `tabProduct Bundle` pb
			join `tabProduct Bundle Item` pbi on pbi.parent = pb.name
			join `tabItem` i on i.name = pbi.item_code and i.is_stock_item = 1
			left join `tabBin` b on b.item_code = pbi.item_code and b.warehouse = %s
			where pb.disabled = 0 and ifnull(b.actual_qty, 0) <= 0""",
			s.kitchen_warehouse,
			as_dict=True,
		)
		short_rows = [r for r in rows if r.item_code not in prepared_items]
		if short_rows:
			dishes = sorted({r.dish for r in short_rows})
			items = sorted({r.item_code for r in short_rows})
			self.warn(
				area,
				_("Ingredients out of stock in the kitchen: {0}. Dishes that would fail at settlement: {1}").format(
					short(items), short(dishes)
				),
			)
		else:
			self.ok(area, _("All dish ingredients have stock in the kitchen"))


def short(names, limit=15):
	names = list(names)
	text = ", ".join(names[:limit])
	if len(names) > limit:
		text += " " + _("and {0} more").format(len(names) - limit)
	return text
