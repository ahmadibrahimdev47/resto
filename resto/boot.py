import frappe


def boot_session(bootinfo):
	"""Settings the Resto screens read without a server call (food cost colors)."""
	if frappe.session.user == "Guest":
		return
	from resto.resto.food_cost import get_levels

	bootinfo.resto = {"food_cost_levels": get_levels()}
