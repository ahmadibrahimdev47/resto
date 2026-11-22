"""A Recipe tab on Item: make or edit the item's Resto Recipe from the item screen.

The Resto Recipe stays the only place the recipe is stored. The tab fields on Item are a view:
- onload fills them from the recipe;
- validate takes them out of the Item (nothing of the recipe is stored on Item rows or columns);
- on_update creates or updates the recipe in the same transaction, so a recipe error stops the
  Item save too.
An Item saved by code or import has empty tab fields and leaves the recipe alone.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.doctype.resto_recipe.resto_recipe import MENU, PREPARED

P = "resto_recipe_"
TYPE, QTY, PAR, ROWS, SIDES, PREP, STAMP, CHANGED = (
	P + "type",
	P + "quantity",
	P + "par_qty",
	P + "items",
	P + "sides",
	P + "preparation",
	P + "modified",
	P + "changed",
)
COSTS = {
	P + "total_cost": "total_cost",
	P + "cost_per_unit": "cost_per_unit",
	P + "selling_rate": "selling_rate",
	P + "food_cost_percent": "food_cost_percent",
}
ROW_FIELDS = ("item_code", "qty", "uom", "note")
IS_PREP = f"eval:!doc.has_variants && doc.{TYPE}=='{PREPARED}'"
IS_MENU = f"eval:!doc.has_variants && doc.{TYPE}=='{MENU}'"
HAS_TYPE = f"eval:!doc.has_variants && doc.{TYPE}"
CURRENCY = "Company:company:default_currency"
NEEDS = "resto_needs_recipe"


def _fields():
	f = []

	def add(fieldname, fieldtype, **kw):
		if f:
			kw.setdefault("insert_after", f[-1]["fieldname"])
		f.append({"fieldname": fieldname, "fieldtype": fieldtype, **kw})

	add(P + "tab", "Tab Break", label="Recipe")
	# Stored on Item (not part of the recipe view): which items should have a recipe.
	add(NEEDS, "Check", label="Needs a Recipe", in_standard_filter=1,
		description="Ticked items are listed in Recipe Status on the recipe screen. Saving a recipe ticks it.")
	add(P + "note", "HTML", label="Recipe Note")
	add(TYPE, "Select", label="Recipe Type", options=f"\n{PREPARED}\n{MENU}", depends_on="eval:!doc.has_variants",
		description="Prepared ingredient: a stock item made in the kitchen. Menu dish: a non-stock item sold at the POS.")
	add(QTY, "Float", label="Yield Quantity", depends_on=IS_PREP,
		description="How much this recipe produces, in the item's stock unit.")
	add(PAR, "Float", label="Daily Par Level", depends_on=IS_PREP,
		description="How much of this should be ready in the kitchen at the start of the day. Used by Daily Prep.")
	add(P + "column", "Column Break")
	add(P + "link", "HTML", label="Recipe Link")
	add(P + "rows_section", "Section Break", depends_on=HAS_TYPE)
	add(ROWS, "Table", label="Ingredients", options="Resto Recipe Item")
	add(SIDES, "Table", label="Served With", options="Resto Recipe Item", depends_on=IS_MENU,
		description="Sides and packaging served with this dish (rice, bread, salad, sauces, box). They are not taken when this dish is part of another dish.")
	add(P + "cost_section", "Section Break", label="Cost", depends_on=HAS_TYPE)
	add(P + "total_cost", "Currency", label="Recipe Cost", options=CURRENCY, read_only=1)
	add(P + "cost_per_unit", "Currency", label="Cost per Unit", options=CURRENCY, read_only=1, depends_on=IS_PREP)
	add(P + "cost_column", "Column Break")
	add(P + "selling_rate", "Currency", label="Selling Price", options=CURRENCY, read_only=1, depends_on=IS_MENU)
	add(P + "food_cost_percent", "Percent", label="Food Cost %", read_only=1, depends_on=IS_MENU)
	add(P + "preparation_section", "Section Break", label="Preparation", depends_on=HAS_TYPE)
	add(PREP, "Text Editor", label="Preparation Method")
	add(STAMP, "Data", label="Recipe Modified", hidden=1, read_only=1, no_copy=1)
	# Set by the form when the user edits the tab; an untouched tab never rewrites the recipe.
	add(CHANGED, "Check", label="Recipe Changed", hidden=1, no_copy=1)
	for d in f:
		d.setdefault("no_copy", 0)
	return f


ITEM_FIELDS = {"Item": _fields()}


# --------------------------------------------------------------------------- Item hooks


def onload(doc, method=None):
	"""Show the item's recipe in the tab."""
	if doc.is_new() or not frappe.db.exists("Resto Recipe", doc.name):
		return
	recipe = frappe.get_doc("Resto Recipe", doc.name)
	_fill(doc, recipe)


def validate(doc, method=None):
	"""Take the tab out of the Item; on_update writes it to the recipe."""
	data = _take(doc)
	if not data:
		return
	if doc.has_variants:
		frappe.throw(_("{0} is a template. Make a recipe for each size (variant) instead.").format(frappe.bold(doc.name)))
	if not (frappe.has_permission("Resto Recipe", "write") or frappe.has_permission("Resto Recipe", "create")):
		frappe.throw(_("You are not allowed to change recipes"), frappe.PermissionError)
	doc.flags.resto_recipe = data
	doc.set(NEEDS, 1)


def on_update(doc, method=None):
	"""Write the tab to the recipe (same transaction as the Item save)."""
	data = doc.flags.pop("resto_recipe", None)
	if not data:
		return
	name = doc.name
	exists = frappe.db.exists("Resto Recipe", name)
	recipe = frappe.get_doc("Resto Recipe", name) if exists else frappe.new_doc("Resto Recipe")
	if exists and _signature(recipe) == data["signature"]:
		return
	if exists and str(recipe.modified) != (data["stamp"] or ""):
		# Someone saved the recipe after this item was opened (or the tab was never loaded).
		frappe.throw(
			_("The recipe of {0} was changed on another screen after you opened this item. Reload the item and try again.").format(
				frappe.bold(name)
			)
		)

	recipe.update(
		{
			"item": name,
			"recipe_type": data["recipe_type"],
			"quantity": data["quantity"],
			"par_qty": data["par_qty"],
			"preparation": data["preparation"],
		}
	)
	recipe.set("items", data["items"])
	recipe.set("sides", data["sides"] if data["recipe_type"] == MENU else [])
	recipe.save() if exists else recipe.insert()

	# The recipe may set default_bom / allow_negative_stock on the Item: keep the form current.
	for field in ("modified", "default_bom", "allow_negative_stock"):
		if doc.meta.has_field(field) or field == "modified":
			doc.set(field, frappe.db.get_value("Item", name, field))


def on_change(doc, method=None):
	"""Show the recipe again in the saved form. Runs after the Item version is saved, so the
	in-memory tab never shows up as a change in the Item's history."""
	if frappe.db.exists("Resto Recipe", doc.name):
		_fill(doc, frappe.get_doc("Resto Recipe", doc.name))


# --------------------------------------------------------------------------- helpers


def _rows(rows):
	return [{f: r.get(f) for f in ROW_FIELDS} for r in rows or [] if r.get("item_code")]


def _signature(recipe_or_data):
	g = recipe_or_data.get
	return (
		g("recipe_type"),
		flt(g("quantity"), 6) if g("recipe_type") == PREPARED else 1,
		flt(g("par_qty"), 6),
		(g("preparation") or "").strip(),
		tuple((r["item_code"], flt(r["qty"], 6), r["uom"] or "", r["note"] or "") for r in _rows(g("items"))),
		tuple((r["item_code"], flt(r["qty"], 6), r["uom"] or "", r["note"] or "") for r in _rows(g("sides"))),
	)


def _take(doc):
	"""Read the tab and clear it from the Item. None when the tab is empty."""
	recipe_type = doc.get(TYPE)
	data = None
	if recipe_type and (cint(doc.get(CHANGED)) or doc.is_new()):
		data = {
			"recipe_type": recipe_type,
			"quantity": flt(doc.get(QTY)) or 1,
			"par_qty": flt(doc.get(PAR)),
			"preparation": doc.get(PREP),
			"items": _rows(doc.get(ROWS)),
			"sides": _rows(doc.get(SIDES)),
			"stamp": doc.get(STAMP),
		}
		data["signature"] = _signature(data)
	for field in (TYPE, PREP, STAMP):
		doc.set(field, None)
	for field in (QTY, PAR, CHANGED, *COSTS):
		doc.set(field, 0)
	doc.set(ROWS, [])
	doc.set(SIDES, [])
	return data


def _fill(doc, recipe):
	"""Copy the recipe into the tab, in memory only. Rows get throw-away names that never reach
	the database: validate always clears the tables before Item rows are written."""
	doc.set(TYPE, recipe.recipe_type)
	doc.set(QTY, recipe.quantity)
	doc.set(PAR, recipe.par_qty)
	doc.set(PREP, recipe.preparation)
	doc.set(STAMP, str(recipe.modified))
	for field, source in COSTS.items():
		doc.set(field, recipe.get(source))
	for table, source in ((ROWS, recipe.items), (SIDES, recipe.sides)):
		doc.set(table, [])
		for r in source:
			row = doc.append(table, {f: r.get(f) for f in (*ROW_FIELDS, "item_name", "stock_uom", "conversion_factor", "stock_qty", "rate", "amount", "is_sub_recipe")})
			row.name = "resto-view-" + frappe.generate_hash(length=10)
	doc.set_onload("resto_recipe_active", cint(recipe.is_active))
