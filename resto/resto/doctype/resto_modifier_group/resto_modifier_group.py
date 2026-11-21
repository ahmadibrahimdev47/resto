import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

CACHE_KEY = "resto_modifier_groups"


class RestoModifierGroup(Document):
	def validate(self):
		if cint(self.min_select) < 0 or cint(self.max_select) < 0:
			frappe.throw(_("Minimum and maximum choices cannot be negative"))
		if self.max_select and cint(self.min_select) > cint(self.max_select):
			frappe.throw(_("Minimum choices cannot be more than maximum choices"))
		names = set()
		for row in self.options:
			if row.option_name in names:
				frappe.throw(_("Option {0} is listed twice").format(frappe.bold(row.option_name)))
			names.add(row.option_name)
			if flt(row.price) < 0:
				frappe.throw(_("Row {0}: price cannot be negative").format(row.idx))
			if flt(row.price) and not row.item:
				frappe.throw(
					_("Row {0}: option {1} has a price, so it needs an item to bill it as").format(
						row.idx, frappe.bold(row.option_name)
					)
				)
		for row in self.applies_to:
			if bool(row.item_group) == bool(row.item_code):
				frappe.throw(_("Shown For row {0}: choose either an item group or an item").format(row.idx))

	def on_update(self):
		frappe.cache.delete_value(CACHE_KEY)

	def on_trash(self):
		frappe.cache.delete_value(CACHE_KEY)


def get_all_groups():
	"""Active modifier groups with options and targets, cached."""
	groups = frappe.cache.get_value(CACHE_KEY)
	if groups is None:
		groups = []
		for name in frappe.get_all("Resto Modifier Group", filters={"is_active": 1}, pluck="name", order_by="name"):
			doc = frappe.get_doc("Resto Modifier Group", name)
			groups.append(
				{
					"name": doc.name,
					"min_select": cint(doc.min_select),
					"max_select": cint(doc.max_select),
					"options": [
						{
							"option_name": o.option_name,
							"price": flt(o.price),
							"item": o.item,
							"is_default": cint(o.is_default),
						}
						for o in doc.options
					],
					"items": [r.item_code for r in doc.applies_to if r.item_code],
					"item_groups": [r.item_group for r in doc.applies_to if r.item_group],
				}
			)
		frappe.cache.set_value(CACHE_KEY, groups)
	return groups


def groups_for_item(item_code):
	"""Modifier groups shown for an item (by item, or its item group and parents)."""
	chain = []
	group = frappe.get_cached_value("Item", item_code, "item_group")
	while group and group not in chain:
		chain.append(group)
		group = frappe.get_cached_value("Item Group", group, "parent_item_group")
	return [
		g for g in get_all_groups() if item_code in g["items"] or any(ig in chain for ig in g["item_groups"])
	]
