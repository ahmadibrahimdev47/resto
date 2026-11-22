import frappe


def boot_session(bootinfo):
	"""Settings the Resto screens read without a server call (food cost colors)."""
	if frappe.session.user == "Guest":
		return
	from resto.resto.food_cost import get_settings

	# {"target": settings target, "yellow": step, "orange": step}; a recipe may have its own target.
	bootinfo.resto = {"food_cost": get_settings()}
