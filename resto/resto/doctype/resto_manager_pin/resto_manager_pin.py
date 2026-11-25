import frappe
from frappe import _
from frappe.model.document import Document


class RestoManagerPIN(Document):
	"""A floor user's PIN, set from the floor screen only: a manager's approves (D-204), everyone's
	unlocks their locked floor screen (D-216)."""

	def validate(self):
		if not self.flags.resto_pin:
			frappe.throw(_("A PIN is set from the floor screen only"))
