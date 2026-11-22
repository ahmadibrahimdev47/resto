import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from resto.resto.costing import get_selling_rate, get_unit_cost
from resto.resto.doctype.resto_settings.resto_settings import get_settings

PREPARED = "Prepared Ingredient"
MENU = "Menu Dish"

# Quantities are compared and written with this many decimals.
QTY_PRECISION = 6


class RestoRecipe(Document):
	def validate(self):
		self.set_defaults()
		self.validate_item()
		self.set_row_details()
		self.validate_rows()
		if self.recipe_type == MENU:
			# Raises on a cycle (a dish that ends up containing itself).
			get_flat_components(self)
		self.calculate_cost()
		if self.recipe_type == MENU and self.selling_rate:
			from resto.resto.food_cost import warn

			warn(self.food_cost_percent, self.target_food_cost_percent)

	def on_update(self):
		self.flag_item()
		if self.flags.skip_sync or not self.is_active:
			return

		if self.recipe_type == PREPARED:
			self.sync_bom()
			self.set_item_negative_stock()
		else:
			self.sync_product_bundle()

		self.update_parent_recipes()

	# ------------------------------------------------------------------ validation

	def set_defaults(self):
		if not self.company:
			self.company = get_settings().company
		if self.recipe_type == MENU:
			self.quantity = 1
			self.bom = None
		else:
			self.set("sides", [])
			self.product_bundle = None
		self.uom = frappe.db.get_value("Item", self.item, "stock_uom")
		# A side group belongs to a side (Served With), not to a main ingredient.
		for row in self.items:
			row.side_group = None

	def validate_item(self):
		item = frappe.db.get_value(
			"Item",
			self.item,
			["is_stock_item", "has_variants", "disabled", "is_fixed_asset"],
			as_dict=True,
		)
		if not item:
			frappe.throw(_("Item {0} does not exist").format(frappe.bold(self.item)))
		if item.disabled:
			frappe.throw(_("Item {0} is disabled").format(frappe.bold(self.item)))
		if item.is_fixed_asset:
			frappe.throw(_("Item {0} is a fixed asset").format(frappe.bold(self.item)))
		if item.has_variants:
			frappe.throw(
				_("Item {0} is a template. Make a recipe for each size (variant) instead.").format(
					frappe.bold(self.item)
				)
			)

		if self.recipe_type == PREPARED and not item.is_stock_item:
			frappe.throw(
				_("A prepared ingredient must be a stock item. Tick Maintain Stock on item {0}.").format(
					frappe.bold(self.item)
				)
			)
		if self.recipe_type == MENU and item.is_stock_item:
			frappe.throw(
				_("A menu dish must not be a stock item. Untick Maintain Stock on item {0}.").format(
					frappe.bold(self.item)
				)
			)

		if flt(self.quantity) <= 0:
			frappe.throw(_("Yield Quantity must be greater than zero"))

	def set_row_details(self, strict=True):
		"""strict=False (live preview while typing): unknown items and units count as zero."""
		from erpnext.stock.get_item_details import get_conversion_factor

		for row in self.all_rows():
			item = row.item_code and frappe.db.get_value(
				"Item", row.item_code, ["item_name", "stock_uom"], as_dict=True
			)
			if not item:
				if not strict:
					row.conversion_factor = row.stock_qty = 0
					continue
				frappe.throw(_("Row {0}: Item {1} does not exist").format(row.idx, frappe.bold(row.item_code)))

			row.item_name = item.item_name
			row.stock_uom = item.stock_uom
			row.uom = row.uom or item.stock_uom
			row.conversion_factor = flt(
				(get_conversion_factor(row.item_code, row.uom, throw_if_missing=strict) or {}).get("conversion_factor")
			)
			row.stock_qty = flt(flt(row.qty) * row.conversion_factor, QTY_PRECISION)
			row.is_sub_recipe = 1 if get_active_recipe(row.item_code, MENU) else 0

	def validate_rows(self):
		if not self.items:
			frappe.throw(_("Add at least one ingredient"))

		for row in self.all_rows():
			label = _("Row {0} in {1}").format(row.idx, _(self.meta.get_label(row.parentfield)))

			if flt(row.qty) <= 0:
				frappe.throw(_("{0}: quantity must be greater than zero").format(label))
			if row.item_code == self.item:
				frappe.throw(_("{0}: a recipe cannot contain its own item").format(label))

			item = frappe.db.get_value(
				"Item",
				row.item_code,
				["is_stock_item", "has_variants", "disabled", "is_fixed_asset"],
				as_dict=True,
			)
			if item.disabled or item.has_variants or item.is_fixed_asset:
				frappe.throw(
					_("{0}: item {1} is disabled, a template or a fixed asset").format(
						label, frappe.bold(row.item_code)
					)
				)

			if self.recipe_type == PREPARED:
				if not item.is_stock_item:
					frappe.throw(
						_("{0}: ingredient {1} must be a stock item").format(label, frappe.bold(row.item_code))
					)
			elif not item.is_stock_item and not row.is_sub_recipe:
				# POSNext checks stock for every packed row, non-stock ones included, so a
				# non-stock component would make every sale of this dish fail.
				frappe.throw(
					_(
						"{0}: {1} is not a stock item and has no menu dish recipe. Every component of a menu dish must be a stock item."
					).format(label, frappe.bold(row.item_code))
				)

	# ------------------------------------------------------------------ cost

	def calculate_cost(self):
		memo = {}
		main_cost = 0
		total_cost = 0
		for row in self.all_rows():
			if not row.item_code:
				row.rate = row.amount = 0
				continue
			row.rate = flt(get_unit_cost(row.item_code, memo, stack=(self.item,)))
			row.amount = flt(row.rate * flt(row.stock_qty))
			total_cost += row.amount
			if row.parentfield == "items":
				main_cost += row.amount

		self.main_cost = main_cost
		self.total_cost = total_cost
		self.cost_per_unit = total_cost / flt(self.quantity or 1)

		if self.recipe_type == MENU:
			self.selling_rate = get_selling_rate(self.item) if self.item else 0
			self.food_cost_percent = (
				flt(total_cost / self.selling_rate * 100, 2) if self.selling_rate else 0
			)
		else:
			self.selling_rate = 0
			self.food_cost_percent = 0

		self.cost_updated_on = now_datetime()

	def update_cost_fields(self):
		"""Recalculate cost and store it without a full save (no sync, no version)."""
		self.calculate_cost()
		for row in self.all_rows():
			row.db_set({"rate": row.rate, "amount": row.amount}, update_modified=False)
		self.db_set(
			{
				"main_cost": self.main_cost,
				"total_cost": self.total_cost,
				"cost_per_unit": self.cost_per_unit,
				"selling_rate": self.selling_rate,
				"food_cost_percent": self.food_cost_percent,
				"cost_updated_on": self.cost_updated_on,
			},
			update_modified=False,
		)

	# ------------------------------------------------------------------ sync

	def sync_bom(self):
		"""Keep a submitted default BOM equal to this recipe. A new BOM is made only on change."""
		components = aggregate(self.items)
		current = self.bom or frappe.db.get_value("Item", self.item, "default_bom")

		if current and bom_matches(current, self.item, self.quantity, components):
			if not frappe.db.get_value("BOM", current, "is_default"):
				bom = frappe.get_doc("BOM", current)
				bom.db_set("is_default", 1)
				bom.manage_default_bom()
			if self.bom != current:
				self.db_set("bom", current, update_modified=False)
			return

		stock_uom = frappe.db.get_value("Item", self.item, "stock_uom")
		bom = frappe.new_doc("BOM")
		bom.update(
			{
				"item": self.item,
				"company": self.company,
				"quantity": self.quantity,
				"uom": stock_uom,
				"currency": frappe.get_cached_value("Company", self.company, "default_currency"),
				"is_active": 1,
				"is_default": 1,
				"with_operations": 0,
				"rm_cost_as_per": "Valuation Rate",
			}
		)
		for item_code, qty in components.items():
			item_stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
			bom.append(
				"items",
				{
					"item_code": item_code,
					"qty": qty,
					"uom": item_stock_uom,
					"stock_uom": item_stock_uom,
					"conversion_factor": 1,
					"stock_qty": qty,
				},
			)
		bom.flags.ignore_permissions = True
		bom.insert()
		bom.submit()
		self.db_set("bom", bom.name, update_modified=False)
		frappe.msgprint(
			_("BOM {0} created and set as default").format(frappe.get_desk_link("BOM", bom.name)),
			alert=True,
			indicator="green",
		)

	def set_item_negative_stock(self):
		if not get_settings().prepared_allow_negative:
			return
		if not frappe.get_meta("Item").has_field("allow_negative_stock"):
			return
		if not frappe.db.get_value("Item", self.item, "allow_negative_stock"):
			frappe.db.set_value("Item", self.item, "allow_negative_stock", 1)

	def sync_product_bundle(self):
		"""Keep a flat Product Bundle equal to this dish's components."""
		components = get_flat_components(self)

		if frappe.db.exists("Product Bundle", self.item):
			bundle = frappe.get_doc("Product Bundle", self.item)
			if not bundle.disabled and aggregate(bundle.items) == components:
				if self.product_bundle != bundle.name:
					self.db_set("product_bundle", bundle.name, update_modified=False)
				return
		else:
			bundle = frappe.new_doc("Product Bundle")
			bundle.new_item_code = self.item

		bundle.disabled = 0
		bundle.description = self.item_name
		bundle.set("items", [])
		for item_code, qty in components.items():
			bundle.append(
				"items",
				{
					"item_code": item_code,
					"qty": qty,
					"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
				},
			)
		bundle.flags.ignore_permissions = True
		bundle.save()
		self.db_set("product_bundle", bundle.name, update_modified=False)

	def update_parent_recipes(self):
		"""Re-save recipes that use this item, so their cost and bundle follow this change.

		Recursion ends because validate() rejects recipe loops.
		"""
		parents = frappe.get_all(
			"Resto Recipe Item",
			filters={"parenttype": "Resto Recipe", "item_code": self.item, "parent": ["!=", self.name]},
			pluck="parent",
			distinct=True,
		)
		for name in parents:
			parent = frappe.get_doc("Resto Recipe", name)
			if not parent.is_active:
				continue
			parent.flags.ignore_permissions = True
			parent.save()

	@frappe.whitelist()
	def update_cost(self):
		self.check_permission("write")
		self.update_cost_fields()

	# ------------------------------------------------------------------ helpers

	def flag_item(self):
		"""An item with a recipe is an item that needs one (Recipe Status lists it)."""
		if frappe.get_meta("Item").has_field("resto_needs_recipe") and not frappe.db.get_value(
			"Item", self.item, "resto_needs_recipe"
		):
			frappe.db.set_value("Item", self.item, "resto_needs_recipe", 1, update_modified=False)

	def all_rows(self):
		return list(self.get("items") or []) + list(self.get("sides") or [])


@frappe.whitelist(methods=["POST"])
def preview_cost(doc):
	"""Costs of the recipe as it is on the screen, saved or not, for the form to show while typing.

	Same rules as saving (calculate_cost); nothing is written. Saving still recalculates and stores.
	"""
	doc = frappe.get_doc(frappe.parse_json(doc))
	if doc.doctype != "Resto Recipe":
		frappe.throw(_("Not a recipe"))
	if not (frappe.has_permission("Resto Recipe", "write") or frappe.has_permission("Resto Recipe", "create")):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if doc.recipe_type == MENU:
		doc.quantity = 1
	doc.set_row_details(strict=False)
	doc.calculate_cost()
	return {
		"main_cost": doc.main_cost,
		"total_cost": doc.total_cost,
		"cost_per_unit": doc.cost_per_unit,
		"selling_rate": doc.selling_rate,
		"food_cost_percent": doc.food_cost_percent,
		"rows": [
			{
				"name": row.name,
				"parentfield": row.parentfield,
				"idx": row.idx,
				"rate": row.rate,
				"amount": row.amount,
				"stock_qty": row.stock_qty,
				"conversion_factor": row.conversion_factor,
			}
			for row in doc.all_rows()
		],
	}


def get_active_recipe(item_code, recipe_type=None):
	filters = {"item": item_code, "is_active": 1}
	if recipe_type:
		filters["recipe_type"] = recipe_type
	return frappe.db.get_value("Resto Recipe", filters, "name")


def aggregate(rows):
	"""Sum quantities per item code in stock units, keeping first-seen order."""
	out = {}
	for row in rows:
		qty = row.get("stock_qty") if row.get("stock_qty") is not None else row.get("qty")
		out[row.item_code] = flt(out.get(row.item_code, 0) + flt(qty), QTY_PRECISION)
	return out


def get_flat_components(doc, include_sides=True, multiplier=1.0, stack=None, out=None):
	"""Expand a menu dish into stock items.

	A component that is itself a menu dish is replaced by that dish's main ingredients
	(not its sides), recursively. Raises on a cycle.
	"""
	stack = (*(stack or ()), doc.item)
	out = {} if out is None else out

	rows = list(doc.get("items") or [])
	if include_sides:
		rows += list(doc.get("sides") or [])

	for row in rows:
		qty = flt(row.stock_qty if row.stock_qty is not None else row.qty) * multiplier
		sub_name = get_active_recipe(row.item_code, MENU)
		if sub_name:
			if row.item_code in stack:
				frappe.throw(
					_("Recipe loop: {0}").format(" → ".join([*stack, row.item_code])),
					title=_("Recipe Loop"),
				)
			sub = frappe.get_doc("Resto Recipe", sub_name)
			get_flat_components(sub, include_sides=False, multiplier=qty, stack=stack, out=out)
		else:
			out[row.item_code] = flt(out.get(row.item_code, 0) + qty, QTY_PRECISION)

	return out


def bom_matches(bom_name, item, quantity, components):
	bom = frappe.db.get_value(
		"BOM", bom_name, ["item", "docstatus", "is_active", "quantity", "with_operations"], as_dict=True
	)
	if not bom or bom.item != item or bom.docstatus != 1 or not bom.is_active or bom.with_operations:
		return False
	if flt(bom.quantity, QTY_PRECISION) != flt(quantity, QTY_PRECISION):
		return False
	rows = frappe.get_all(
		"BOM Item",
		filters={"parent": bom_name, "parenttype": "BOM", "parentfield": "items"},
		fields=["item_code", "stock_qty"],
		order_by="idx asc",
	)
	return aggregate([frappe._dict(r) for r in rows]) == components


@frappe.whitelist()
def recipe_status():
	"""Items ticked "Needs a Recipe", each with its recipe state (Active / Inactive / Missing)."""
	frappe.has_permission("Resto Recipe", "read", throw=True)
	if not frappe.get_meta("Item").has_field("resto_needs_recipe"):
		return {"rows": [], "missing": 0}
	items = frappe.get_all(
		"Item",
		filters={"resto_needs_recipe": 1, "disabled": 0, "has_variants": 0},
		fields=["name", "item_name", "item_group", "is_stock_item", "stock_uom"],
		order_by="item_group, item_name",
	)
	recipes = {
		r.item: r
		for r in frappe.get_all(
			"Resto Recipe",
			filters={"item": ["in", [i.name for i in items] or [""]]},
			fields=["name", "item", "is_active", "recipe_type", "total_cost", "cost_per_unit", "food_cost_percent", "target_food_cost_percent"],
		)
	}
	rows = []
	for i in items:
		r = recipes.get(i.name)
		rows.append(
			{
				"item": i.name,
				"item_name": i.item_name,
				"item_group": i.item_group,
				"uom": i.stock_uom,
				"expected_type": PREPARED if i.is_stock_item else MENU,
				"recipe": r.name if r else None,
				"status": "Missing" if not r else ("Active" if r.is_active else "Inactive"),
				"recipe_type": r.recipe_type if r else None,
				"cost": (r.cost_per_unit if r.recipe_type == PREPARED else r.total_cost) if r else None,
				"food_cost_percent": r.food_cost_percent if r and r.recipe_type == MENU else None,
				"target_food_cost_percent": r.target_food_cost_percent if r and r.recipe_type == MENU else None,
			}
		)
	return {"rows": rows, "missing": sum(1 for r in rows if r["status"] == "Missing")}
