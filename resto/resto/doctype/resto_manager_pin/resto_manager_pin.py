import frappe
from frappe import _
from frappe.model.document import Document


class RestoManagerPIN(Document):
	"""A manager's PIN for approvals on the floor (D-204), set from the floor screen only."""

	def validate(self):
		if not self.flags.resto_pin:
			frappe.throw(_("A PIN is set from the floor screen only"))
