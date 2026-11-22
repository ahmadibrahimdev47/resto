import frappe
from frappe.utils import flt


def boot_session(bootinfo):
	"""Settings the Resto screens read without a server call (food cost colors)."""
	if frappe.session.user == "Guest":
		return
	alert = frappe.db.get_single_value("Resto Settings", "food_cost_alert_percent") if frappe.db.exists(
		"DocType", "Resto Settings"
	) else None
	bootinfo.resto = {"food_cost_alert": flt(alert) or 35}
