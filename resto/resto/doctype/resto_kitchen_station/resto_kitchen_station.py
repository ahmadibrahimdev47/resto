import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from resto.resto.routing import clear_cache


class RestoKitchenStation(Document):
	def validate(self):
		if not self.print_method:
			self.print_method = "Server Printer" if self.printer else "Browser Button"
		if self.print_method == "Server Printer" and not self.printer:
			frappe.throw(_("Choose the kitchen printer, or another printing method"))
		if cint(self.auto_print_minutes) < 0:
			self.auto_print_minutes = 0
		seen = set()
		for row in self.item_groups:
			if row.item_group in seen:
				frappe.throw(_("Item Group {0} is listed twice").format(frappe.bold(row.item_group)))
			seen.add(row.item_group)
		seen = set()
		for row in self.items:
			if row.item_code in seen:
				frappe.throw(_("Item {0} is listed twice").format(frappe.bold(row.item_code)))
			seen.add(row.item_code)

	def on_update(self):
		if self.is_default:
			# Only one default station.
			frappe.db.set_value(
				"Resto Kitchen Station",
				{"is_default": 1, "name": ("!=", self.name)},
				"is_default",
				0,
			)
		clear_cache()

	def on_trash(self):
		clear_cache()

	def after_rename(self, old, new, merge=False):
		clear_cache()
