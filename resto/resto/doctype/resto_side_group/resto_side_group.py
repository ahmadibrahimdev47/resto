import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class RestoSideGroup(Document):
	# Not renamed: its name is stored in the sides of open order lines.
	def validate(self):
		from resto.resto.sides import clear_cache

		clear_cache()
		if cint(self.max_portions) < 1:
			frappe.throw(_("Maximum portions per dish must be at least 1"))
		seen = set()
		for row in self.options:
			if row.item in seen:
				frappe.throw(_("Row {0}: {1} is listed twice").format(row.idx, frappe.bold(row.item)))
			seen.add(row.item)
			self.set_portion_qty(row)
			if flt(row.portion_qty) <= 0:
				frappe.throw(_("Row {0}: the quantity of one portion must be more than zero").format(row.idx))
			item = frappe.get_cached_value("Item", row.item, ["disabled", "has_variants", "has_serial_no", "has_batch_no"], as_dict=True)
			if not item or item.disabled or item.has_variants:
				frappe.throw(_("Row {0}: {1} is disabled or a template").format(row.idx, frappe.bold(row.item)))
			if item.has_serial_no or item.has_batch_no:
				frappe.throw(_("Row {0}: {1} has serial or batch numbers and cannot be served as a side").format(row.idx, frappe.bold(row.item)))
			if flt(row.get("extra_price")) < 0:
				frappe.throw(_("Row {0}: the price of an extra portion cannot be negative").format(row.idx))
			if flt(row.get("extra_price")) > 0:
				self.validate_billing_item(row)
			elif row.get("billing_item"):
				row.billing_item = None  # nothing is billed without a price
			if not _deductible(row.item):
				frappe.throw(
					_("Row {0}: {1} is not a stock item and has no menu dish recipe, so nothing could be deducted").format(
						row.idx, frappe.bold(row.item)
					)
				)

	def set_portion_qty(self, row):
		"""One portion is typed in a recipe unit (D-264): grams for an item stocked by the kilo,
		millilitres by the litre, or a unit the item converts to; what is deducted is kept in the
		stock unit. A row with only the stock quantity (older ones) is read as typed in the stock unit."""
		from resto.resto.units import factor

		stock_uom = frappe.get_cached_value("Item", row.item, "stock_uom")
		if flt(row.entered_qty) > 0:
			row.entered_uom = row.entered_uom or stock_uom
			row.portion_qty = flt(flt(row.entered_qty) * factor(row.item, row.entered_uom), 6)
		elif flt(row.portion_qty) > 0:
			row.entered_qty, row.entered_uom = row.portion_qty, stock_uom
		row.uom = stock_uom

	def validate_billing_item(self, row):
		"""The extra portions' invoice line deducts its item: it must deduct exactly one portion of
		the option (the option's own stock item when one portion is one stock unit, or a menu dish
		whose recipe is one portion)."""
		from resto.resto.sides import extra_billing_error

		if not row.billing_item:
			frappe.throw(_("Row {0}: choose the item the extra portions of {1} are billed as").format(row.idx, frappe.bold(row.item)))
		error = extra_billing_error(row.item, row.portion_qty, row.billing_item)
		if error:
			frappe.throw(_("Row {0}: {1}").format(row.idx, error))


def _deductible(item_code):
	from resto.resto.doctype.resto_recipe.resto_recipe import MENU, get_active_recipe

	return bool(frappe.get_cached_value("Item", item_code, "is_stock_item") or get_active_recipe(item_code, MENU))
