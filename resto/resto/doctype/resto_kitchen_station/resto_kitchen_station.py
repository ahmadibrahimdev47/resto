import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from resto.resto.routing import clear_cache


class RestoKitchenStation(Document):
	def validate(self):
		if not self.print_method or (self.is_new() and self.printer and self.print_method == "Browser Button"):
			# A station made with only a printer (an import, an older script) prints on it.
			self.print_method = "Server Printer" if self.printer else "Browser Button"
		if self.print_method == "Server Printer" and not self.printer:
			frappe.throw(_("Choose the kitchen printer, or another printing method"))
		if cint(self.auto_print_minutes) <= 0:
			from resto.resto.kitchen import AUTO_PRINT_MINUTES

			self.auto_print_minutes = AUTO_PRINT_MINUTES
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
