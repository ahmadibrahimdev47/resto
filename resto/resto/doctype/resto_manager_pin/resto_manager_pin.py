import frappe
from frappe import _
from frappe.model.document import Document


class RestoManagerPIN(Document):
	"""A floor user's PINs, set from the floor screen only: a manager's approval PIN (D-204), and
	everyone's lock PIN that opens their locked floor screen (D-216)."""

	def validate(self):
		if not self.flags.resto_pin:
			frappe.throw(_("A PIN is set from the floor screen only"))
