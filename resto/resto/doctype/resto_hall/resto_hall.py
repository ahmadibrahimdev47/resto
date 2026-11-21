import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class RestoHall(Document):
	def validate(self):
		if cint(self.grid_columns) < 1:
			self.grid_columns = 6
