import frappe
from frappe import _
from frappe.model.document import Document


class RestoKitchenTicket(Document):
	def on_trash(self):
		frappe.throw(_("Kitchen tickets cannot be deleted"))
