"""The course value "Main" became "Main Course".

A bare "Main" translation collided with ERPNext's default cost center name.
"""

import frappe


def execute():
	for doctype in ("Resto Order Item", "Resto Kitchen Ticket Item"):
		if frappe.db.has_column(doctype, "course"):
			frappe.db.sql(f"update `tab{doctype}` set course = 'Main Course' where course = 'Main'")
