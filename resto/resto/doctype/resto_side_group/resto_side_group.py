import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class RestoSideGroup(Document):
	# Not renamed: its name is stored in the sides of open order lines.
	def validate(self):
		if cint(self.max_portions) < 1:
			frappe.throw(_("Maximum portions per dish must be at least 1"))
		seen = set()
		for row in self.options:
			if row.item in seen:
				frappe.throw(_("Row {0}: {1} is listed twice").format(row.idx, frappe.bold(row.item)))
			seen.add(row.item)
			if flt(row.portion_qty) <= 0:
				frappe.throw(_("Row {0}: the quantity of one portion must be more than zero").format(row.idx))
			item = frappe.get_cached_value("Item", row.item, ["disabled", "has_variants", "has_serial_no", "has_batch_no"], as_dict=True)
			if not item or item.disabled or item.has_variants:
				frappe.throw(_("Row {0}: {1} is disabled or a template").format(row.idx, frappe.bold(row.item)))
			if item.has_serial_no or item.has_batch_no:
				frappe.throw(_("Row {0}: {1} has serial or batch numbers and cannot be served as a side").format(row.idx, frappe.bold(row.item)))
			if not _deductible(row.item):
				frappe.throw(
					_("Row {0}: {1} is not a stock item and has no menu dish recipe, so nothing could be deducted").format(
						row.idx, frappe.bold(row.item)
					)
				)


def _deductible(item_code):
	from resto.resto.doctype.resto_recipe.resto_recipe import MENU, get_active_recipe

	return bool(frappe.get_cached_value("Item", item_code, "is_stock_item") or get_active_recipe(item_code, MENU))
