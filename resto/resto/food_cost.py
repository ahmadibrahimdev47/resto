"""Food cost % levels of a dish (recipe cost / selling price), set in Resto Settings.

green up to `green`, yellow up to `yellow`, orange up to `orange`, red above that, and "over"
at 100% or more (the dish costs more than it sells for). The same levels color the screens
(recipe_status.js, through boot) and the warning shown when a recipe is saved.
"""

import frappe
from frappe import _
from frappe.utils import flt

DEFAULTS = {"green": 50, "yellow": 65, "orange": 80}
FIELDS = {
	"green": "food_cost_green_upto",
	"yellow": "food_cost_yellow_upto",
	"orange": "food_cost_orange_upto",
}


def get_levels():
	values = {}
	if frappe.db.exists("DocType", "Resto Settings"):
		for key, field in FIELDS.items():
			values[key] = flt(frappe.db.get_single_value("Resto Settings", field))
	levels = {key: values.get(key) or default for key, default in DEFAULTS.items()}
	# A level left lower than the one before it falls back to the defaults as a whole.
	if not levels["green"] < levels["yellow"] < levels["orange"] < 100:
		levels = dict(DEFAULTS)
	return levels


def get_level(percent, levels=None):
	levels = levels or get_levels()
	percent = flt(percent)
	if percent >= 100:
		return "over"
	for key in ("green", "yellow", "orange"):
		if percent <= levels[key]:
			return key
	return "red"


def warn(percent):
	"""Non-blocking message when a dish's food cost is above the green level."""
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install:
		return
	levels = get_levels()
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
	# The level just below this one is the line it crossed.
	crossed = {"yellow": "green", "orange": "yellow", "red": "orange"}[level]
	message = _("Food cost is {0}% of the selling price, above {1}%.").format(
		shown, "%g" % flt(levels[crossed], 1)
	)
	if level == "red":
		frappe.msgprint(message, title=_("Food Cost"), indicator="red")
	else:
		frappe.msgprint(message, indicator=level, alert=True)
