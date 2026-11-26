import frappe


def boot_session(bootinfo):
	"""Settings the Resto screens read without a server call (food cost colors, business day)."""
	if frappe.session.user == "Guest":
		return
	from resto.resto.demand import day_start_hour
	from resto.resto.food_cost import get_settings

	# {"target": settings target, "yellow": step, "orange": step}; a recipe may have its own target.
	from resto.resto.backup_watch import alert_for_user

	bootinfo.resto = {
		"food_cost": get_settings(),
		"business_day_start_hour": day_start_hour(),
		# A late backup, for the red bar (D-256); None for anyone but a manager.
		"backup_alert": _backup_alert(alert_for_user),
	}


def _backup_alert(alert_for_user):
	"""The watch never stops the desk from loading (an unreadable folder, say)."""
	try:
		return alert_for_user()
	except Exception:
		frappe.log_error("Resto backup watch")
		return None
