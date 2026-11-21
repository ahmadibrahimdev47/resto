import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class RestoTable(Document):
	def validate(self):
		if cint(self.seats) < 0:
			frappe.throw(_("Seats cannot be negative"))
		if cint(self.grid_row) < 0 or cint(self.grid_col) < 0:
			frappe.throw(_("Row and column cannot be negative"))

	def on_trash(self):
		if frappe.db.exists("Resto Order", {"resto_table": self.name, "status": ("in", ("Open", "Bill Requested"))}):
			frappe.throw(_("Table {0} has an open order").format(frappe.bold(self.name)))
