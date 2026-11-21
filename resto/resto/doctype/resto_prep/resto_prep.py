import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

from resto.resto.utils import get_settings

PREPARED = "Prepared Ingredient"


class RestoPrep(Document):
	def validate(self):
		settings = get_settings()
		self.company = self.company or settings.company
		self.warehouse = self.warehouse or settings.kitchen_warehouse
		self.source_warehouse = self.source_warehouse or settings.main_warehouse
		for field in ("warehouse", "source_warehouse"):
			wh = self.get(field)
			if wh and frappe.db.get_value("Warehouse", wh, "company") != self.company:
				frappe.throw(_("Warehouse {0} does not belong to company {1}").format(frappe.bold(wh), self.company))
		if self.warehouse and self.warehouse == self.source_warehouse:
			frappe.throw(_("Main Store and Kitchen Warehouse must be different"))

		seen = set()
		for row in self.items:
			recipe = frappe.db.get_value(
				"Resto Recipe", row.recipe, ["recipe_type", "item", "is_active", "bom", "uom", "par_qty"], as_dict=True
			)
			if not recipe or recipe.recipe_type != PREPARED:
				frappe.throw(_("Row {0}: {1} is not a prepared ingredient recipe").format(row.idx, frappe.bold(row.recipe)))
			if not recipe.is_active:
				frappe.throw(_("Row {0}: recipe {1} is not active").format(row.idx, frappe.bold(row.recipe)))
			if row.recipe in seen:
				frappe.throw(_("Row {0}: {1} is listed twice").format(row.idx, frappe.bold(row.recipe)))
			seen.add(row.recipe)
			row.item_code = recipe.item
			row.uom = frappe.get_cached_value("Item", recipe.item, "stock_uom")
			if flt(row.qty) < 0:
				frappe.throw(_("Row {0}: quantity cannot be negative").format(row.idx))

		if not any(flt(d.qty) > 0 for d in self.items):
			frappe.throw(_("Enter a quantity to prepare for at least one row"))

	def before_submit(self):
		# Rows with nothing to prepare are dropped so the document shows what was made.
		self.items = [d for d in self.items if flt(d.qty) > 0]
		for i, d in enumerate(self.items, 1):
			d.idx = i

	def on_submit(self):
		for row in self.items:
			bom = _get_bom(row.recipe, row.item_code)
			se = frappe.new_doc("Stock Entry")
			se.stock_entry_type = "Manufacture"
			se.purpose = "Manufacture"
			se.company = self.company
			se.from_bom = 1
			se.use_multi_level_bom = 0
			se.bom_no = bom
			se.fg_completed_qty = flt(row.qty)
			se.from_warehouse = self.warehouse
			se.to_warehouse = self.warehouse
			se.posting_date = self.posting_date
			se.set_posting_time = 1 if getdate(self.posting_date) != getdate(nowdate()) else 0
			se.remarks = _("Daily prep {0}").format(self.name)
			se.get_items()
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
			row.db_set("stock_entry", se.name)

	def on_cancel(self):
		for row in reversed(self.items):
			if row.stock_entry and frappe.db.get_value("Stock Entry", row.stock_entry, "docstatus") == 1:
				se = frappe.get_doc("Stock Entry", row.stock_entry)
				se.flags.ignore_permissions = True
				se.cancel()

	@frappe.whitelist()
	def get_suggestions(self):
		"""Fill the rows: par level minus what is in the kitchen now (never below zero)."""
		self.check_permission("write")
		settings = get_settings()
		warehouse = self.warehouse or settings.kitchen_warehouse
		if not warehouse:
			frappe.throw(_("Set the Kitchen Warehouse first"))
		self.set("items", [])
		for r in frappe.get_all(
			"Resto Recipe",
			filters={"recipe_type": PREPARED, "is_active": 1, "par_qty": (">", 0), "company": self.company or settings.company},
			fields=["name", "item", "par_qty"],
			order_by="item asc",
		):
			current = flt(frappe.db.get_value("Bin", {"item_code": r.item, "warehouse": warehouse}, "actual_qty"))
			suggested = max(flt(r.par_qty) - current, 0)
			self.append(
				"items",
				{
					"recipe": r.name,
					"item_code": r.item,
					"uom": frappe.get_cached_value("Item", r.item, "stock_uom"),
					"par_qty": r.par_qty,
					"current_qty": current,
					"suggested_qty": suggested,
					"qty": suggested,
				},
			)
		return self.as_dict()

	@frappe.whitelist()
	def make_raw_material_transfer(self):
		"""Draft Material Transfer (main store -> kitchen) for raw materials the prep still needs."""
		self.check_permission("write")
		if not self.source_warehouse or not self.warehouse:
			frappe.throw(_("Set the Main Store and the Kitchen Warehouse first"))
		needed = {}
		for row in self.items:
			if flt(row.qty) <= 0:
				continue
			bom = frappe.get_doc("BOM", _get_bom(row.recipe, row.item_code))
			for it in bom.items:
				needed[it.item_code] = needed.get(it.item_code, 0) + flt(it.stock_qty) * flt(row.qty) / flt(bom.quantity)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.company = self.company
		se.from_warehouse = self.source_warehouse
		se.to_warehouse = self.warehouse
		se.remarks = _("Raw materials for daily prep {0}").format(self.name)
		for item_code, qty in sorted(needed.items()):
			if not frappe.get_cached_value("Item", item_code, "is_stock_item"):
				continue
			in_kitchen = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse}, "actual_qty"))
			short = qty - max(in_kitchen, 0)
			if short > 0.0000001:
				uom = frappe.get_cached_value("Item", item_code, "stock_uom")
				se.append(
					"items",
					{
						"item_code": item_code,
						"qty": short,
						"uom": uom,
						"stock_uom": uom,
						"conversion_factor": 1,
						"s_warehouse": self.source_warehouse,
						"t_warehouse": self.warehouse,
					},
				)
		if not se.items:
			frappe.msgprint(_("The kitchen already has all raw materials needed"))
			return None
		se.insert()
		return se.name


def _get_bom(recipe, item_code):
	bom = frappe.db.get_value("Resto Recipe", recipe, "bom")
	if not bom or frappe.db.get_value("BOM", bom, "docstatus") != 1:
		bom = frappe.db.get_value("Item", item_code, "default_bom")
	if not bom:
		frappe.throw(_("Recipe {0} has no submitted BOM yet").format(frappe.bold(recipe)))
	return bom
