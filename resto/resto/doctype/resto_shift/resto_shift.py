import frappe
from frappe import _
from frappe.model.document import Document


class RestoShift(Document):
	"""A cashier's shift (D-203): opened and closed from the floor screen only (resto.resto.shift),
	so what it counted is never changed by hand afterwards."""

	def validate(self):
		if not self.flags.resto_shift:
			frappe.throw(_("A shift is opened and closed from the floor screen only"))

	def on_trash(self):
		if frappe.db.exists("Sales Invoice", {"resto_shift": self.name}):
			frappe.throw(_("Shift {0} has invoices and cannot be deleted").format(self.name))
