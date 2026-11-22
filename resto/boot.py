import frappe


def boot_session(bootinfo):
	"""Settings the Resto screens read without a server call (food cost colors, business day)."""
	if frappe.session.user == "Guest":
		return
	from resto.resto.demand import day_start_hour
	from resto.resto.food_cost import get_settings

	# {"target": settings target, "yellow": step, "orange": step}; a recipe may have its own target.
	bootinfo.resto = {"food_cost": get_settings(), "business_day_start_hour": day_start_hour()}
