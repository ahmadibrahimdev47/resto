import frappe
from frappe import _
from frappe.model.document import Document


class RestoApprovalLog(Document):
	"""Who approved what on the floor (D-204). Written by the server only; never changed."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("An approval log cannot be changed"))

	def on_trash(self):
		frappe.throw(_("An approval log cannot be deleted"))
