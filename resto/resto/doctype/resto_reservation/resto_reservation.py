import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, getdate, nowdate


class RestoReservation(Document):
	def validate(self):
		if cint(self.guests) < 1:
			frappe.throw(_("Guests must be at least 1"))
		if self.is_new() and getdate(self.reservation_date) < getdate(nowdate()):
			frappe.throw(_("The reservation date is in the past"))
		if self.resto_table:
			hall = frappe.db.get_value("Resto Table", self.resto_table, "hall")
			if self.hall and hall != self.hall:
				frappe.throw(_("Table {0} is not in hall {1}").format(frappe.bold(self.resto_table), frappe.bold(self.hall)))
			self.hall = hall
			self.warn_overlap()

	def warn_overlap(self):
		"""Warn (not block) when the same table is booked within two hours."""
		if self.status != "Booked":
			return
		at = get_datetime(f"{self.reservation_date} {self.reservation_time}")
		for other in frappe.get_all(
			"Resto Reservation",
			filters={
				"resto_table": self.resto_table,
				"reservation_date": self.reservation_date,
				"status": "Booked",
				"name": ("!=", self.name),
			},
			fields=["name", "customer_name", "reservation_time"],
		):
			other_at = get_datetime(f"{self.reservation_date} {other.reservation_time}")
			if abs((other_at - at).total_seconds()) < 2 * 3600:
				frappe.msgprint(
					_("Table {0} is also booked for {1} at {2}").format(
						frappe.bold(self.resto_table), other.customer_name, str(other.reservation_time)[:5]
					),
					indicator="orange",
					alert=True,
				)
