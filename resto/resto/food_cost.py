"""Food cost % levels of a dish (recipe cost / selling price).

Green up to the target, yellow up to target + the yellow step, orange up to target + the orange
step, red above that, and "over" at 100% or more (the dish costs more than it sells for).
The target is the recipe's own Target Food Cost %, or the one in Resto Settings when the recipe
has none; the steps are in Resto Settings. The same levels color the screens
(recipe_status.js, through boot) and the warning shown when a recipe is saved.
"""

import frappe
from frappe import _
from frappe.utils import flt

TARGET_FIELD = "food_cost_alert_percent"
DEFAULT_TARGET = 35
STEPS = {"yellow": 15, "orange": 30}
STEP_FIELDS = {"yellow": "food_cost_yellow_step", "orange": "food_cost_orange_step"}


def get_settings():
	"""The Resto Settings target and steps, with defaults for empty or out-of-order values."""
	values = {}
	if frappe.db.exists("DocType", "Resto Settings"):
		for field in (TARGET_FIELD, *STEP_FIELDS.values()):
			values[field] = flt(frappe.db.get_single_value("Resto Settings", field))
	steps = {key: values.get(field) or STEPS[key] for key, field in STEP_FIELDS.items()}
	if not 0 < steps["yellow"] < steps["orange"]:
		steps = dict(STEPS)
	return {"target": values.get(TARGET_FIELD) or DEFAULT_TARGET, **steps}


def get_levels(target=None, settings=None):
	"""Levels for a dish; `target` is the recipe's own target (empty: the settings target)."""
	settings = settings or get_settings()
	target = flt(target) or settings["target"]
	return {"green": target, "yellow": target + settings["yellow"], "orange": target + settings["orange"]}


def get_level(percent, levels=None):
	levels = levels or get_levels()
	percent = flt(percent)
	if percent >= 100:
		return "over"
	for key in ("green", "yellow", "orange"):
		if percent <= levels[key]:
			return key
	return "red"


def warn(percent, target=None):
	"""Non-blocking message when a dish's food cost is above its target."""
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install:
		return
	levels = get_levels(target)
	level = get_level(percent, levels)
	if level == "green":
		return
	shown = "%g" % flt(percent, 1)
	if level == "over":
		frappe.msgprint(
			_("The dish costs more than its selling price ({0}%).").format(shown),
			title=_("Food Cost"),
			indicator="red",
		)
		return
	message = _("Food cost is {0}% of the selling price, above the {1}% target.").format(
		shown, "%g" % flt(levels["green"], 1)
	)
	if level == "red":
		frappe.msgprint(message, title=_("Food Cost"), indicator="red")
	else:
		frappe.msgprint(message, indicator=level, alert=True)
