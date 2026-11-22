"""Sides served with a dish («يُقدَّم مع»), changed at order time.

A dish recipe's sides are the default: its Product Bundle holds the main ingredients plus those
sides, and ERPNext deducts the bundle with every sale, as it always did. When "Let Staff Change
the Sides Served" is on, the floor screen can change the sides of one dish line, or of several lines
together (one sauce for two dishes: each dish gets its share).

Slots: the recipe's side rows grouped by side group (or, without a group, by item). A slot with a
group offers the group's options and up to its maximum portions per dish; a slot without a group
can only be kept or left out.

A line's choice (Resto Order Item.sides, JSON) is {slot key: {"items": {item: portions per dish}}}.
A choice shared by several lines also has "set" (its id), "n" (how many lines share it), "q" (this
line's quantity when it was shared) and "t" (the total {item: portions} of the set). Nothing stored
means the recipe's sides. Resto Order Item.sides_qty is what the line serves, {item: stock qty per
dish}, frozen when the line goes to the kitchen.

At billing sides_qty becomes `resto_sides` on the Sales Invoice row. The invoice's validate hook
then sets that row's Packed Items to the dish's main ingredients plus the sides served, so ERPNext
posts stock, cost of goods sold and the cost per dish from those rows, exactly as it does for a
bundle. A return takes back the packed rows of the invoice row it returns, each linked to the row it
reverses, and ERPNext values it by its own return rules. Before submit the packed rows are checked
again; anything unexpected stops the bill instead of posting wrong stock.
"""

import json
import math
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.utils import get_settings

EPS = 1e-9
NOT_SENT = "Not Sent"
VOID = "Void"


def enabled():
	return bool(cint(get_settings().get("allow_side_changes")))


def _loads(value, what=None):
	if not value:
		return None
	if not isinstance(value, str):
		return value
	try:
		return json.loads(value)
	except ValueError:
		frappe.throw(_("The sides of {0} could not be read").format(frappe.bold(what or _("a line"))))


def _dumps(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else None


def _num(value):
	"""1, 2, 0.5: a portion count as people write it."""
	value = flt(value, 3)
	return str(int(value)) if value == int(value) else f"{value:g}"


def _same(a, b):
	a, b = a or {}, b or {}
	keys = {k for k, v in a.items() if flt(v) > EPS} | {k for k, v in b.items() if flt(v) > EPS}
	return all(abs(flt(a.get(k)) - flt(b.get(k))) < 1e-6 for k in keys)


# --------------------------------------------------------------------------- kitchen wording


def _lang():
	return frappe.db.get_single_value("System Settings", "language") or "en"


def _t(text, *args):
	"""Order text in the site's language (the kitchen reads it), whoever saved the order."""
	out = _(text, lang=_lang(), context="Resto")
	return out.format(*args) if args else out


def _sep():
	return "، " if _lang().startswith("ar") else ", "


# --------------------------------------------------------------------------- slots


def _menu_recipe(item_code):
	from resto.resto.doctype.resto_recipe.resto_recipe import MENU, get_active_recipe

	name = get_active_recipe(item_code, MENU)
	return frappe.get_cached_doc("Resto Recipe", name) if name else None


def _whole(item_code):
	uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	return bool(uom and cint(frappe.get_cached_value("UOM", uom, "must_be_whole_number")))


def slots(item_code):
	"""The side slots of a dish: {key: slot}. A slot has label, group, max (portions per dish),
	default {item: portions}, portion {item: stock qty of one portion} and names {item: item name}."""
	out = {}
	recipe = _menu_recipe(item_code)
	for row in (recipe.get("sides") if recipe else None) or []:
		qty = flt(row.stock_qty if row.stock_qty is not None else row.qty)
		if qty <= EPS:
			continue
		group = row.get("side_group")
		key = group or f"item:{row.item_code}"
		slot = out.get(key)
		if not slot:
			slot = out[key] = frappe._dict(key=key, group=group, default={}, default_qty={}, portion={}, names={}, max=0)
			if group:
				g = frappe.get_cached_doc("Resto Side Group", group)
				slot.label = g.group_name
				slot.group_max = cint(g.max_portions) or 1
				for o in g.options:
					slot.portion[o.item] = flt(o.portion_qty)
					slot.names[o.item] = o.item_name or o.item
			else:
				slot.label = row.item_name or row.item_code
				slot.group_max = 0
		slot.default[row.item_code] = slot.default.get(row.item_code, 0) + 1
		slot.default_qty[row.item_code] = slot.default_qty.get(row.item_code, 0) + qty
		slot.names[row.item_code] = row.item_name or slot.names.get(row.item_code) or row.item_code
	for slot in out.values():
		# The recipe's own side: one portion is its recipe quantity.
		for item, count in slot.default.items():
			slot.portion[item] = slot.default_qty[item] / count
		# Without a group a side can only be kept or left out.
		slot.max = max(slot.group_max, sum(slot.default.values()))
	return out


def default_choice(item_code):
	return {key: {"items": dict(slot.default)} for key, slot in slots(item_code).items()}


def _slot_label(item_code, key):
	slot = slots(item_code).get(key)
	if slot:
		return slot.label
	if key.startswith("item:"):
		code = key[5:]
		return frappe.get_cached_value("Item", code, "item_name") or code
	return key


def clean_choice(item_code, choice, label=None, strict=True):
	"""A choice checked against the dish's slots, with every slot.

	strict (a change being made): unknown, negative or too many portions are refused. Not strict
	(an order being saved): a choice the recipe or side group no longer allows goes back to the
	recipe's sides, with a message, so the order can still be saved."""
	label = label or item_code
	all_slots = slots(item_code)
	choice = choice or {}
	if not isinstance(choice, dict):
		frappe.throw(_("{0}: the sides could not be read").format(frappe.bold(label)))
	result, reset = {}, []

	def refuse(message):
		if strict:
			frappe.throw(message)

	for key in choice:
		if key not in all_slots:
			refuse(_("{0}: {1} is not a side of this dish").format(frappe.bold(label), frappe.bold(_slot_label(item_code, key))))
			reset.append(_slot_label(item_code, key))
	for key, slot in all_slots.items():
		entry = choice.get(key)
		if entry is None:
			result[key] = {"items": dict(slot.default)}
			continue
		if not isinstance(entry, dict) or not isinstance(entry.get("items") or {}, dict):
			frappe.throw(_("{0}: the sides could not be read").format(frappe.bold(label)))
		items, ok = {}, True
		for item, portions in (entry.get("items") or {}).items():
			portions = flt(portions, 9)
			if portions < -EPS:
				refuse(_("{0}: portions cannot be negative").format(frappe.bold(label)))
				ok = False
			elif item not in slot.portion:
				name = frappe.get_cached_value("Item", item, "item_name") or item
				refuse(_("{0}: {1} is not an option of {2}").format(frappe.bold(label), frappe.bold(name), frappe.bold(slot.label)))
				ok = False
			elif portions > EPS:
				items[item] = portions
		# A shared choice is limited by its total (set_for_lines), not dish by dish.
		if strict and not entry.get("set") and sum(items.values()) > slot.max + 1e-6:
			frappe.throw(
				_("{0}: the most portions of {1} for one dish is {2}").format(frappe.bold(label), frappe.bold(slot.label), slot.max)
			)
		if not ok:
			result[key] = {"items": dict(slot.default)}
			reset.append(slot.label)
			continue
		result[key] = {"items": items}
		if entry.get("set"):
			for field in ("set", "n", "q", "t"):
				if field in entry:
					result[key][field] = entry[field]
	if reset:
		frappe.msgprint(
			_("{0}: the recipe or its side group changed, so {1} went back to the recipe's sides").format(
				frappe.bold(label), ", ".join(reset)
			),
			alert=True,
			indicator="orange",
		)
	return result


def qty_per_dish(item_code, choice):
	"""{item: stock qty per dish} of the sides a (clean) choice serves."""
	out = defaultdict(float)
	all_slots = slots(item_code)
	for key, entry in (choice or default_choice(item_code)).items():
		slot = all_slots.get(key)
		if not slot:
			continue
		for item, portions in (entry.get("items") or {}).items():
			out[item] += flt(portions) * flt(slot.portion.get(item))
	return {k: flt(v, 9) for k, v in out.items() if v > EPS}


def is_default(item_code, choice):
	if not choice:
		return True
	if any(entry.get("set") for entry in choice.values()):
		return False
	actual, default = qty_per_dish(item_code, choice), qty_per_dish(item_code, None)
	return set(actual) == set(default) and all(abs(actual[k] - default[k]) < 1e-6 for k in actual)


# --------------------------------------------------------------------------- order lines


def set_line_sides(row, old=None):
	"""Resolve an unsent order line's sides, server side (called from the order's validate).
	With the setting off a line keeps a change made before, but cannot get a new one."""
	if not slots(row.item_code):
		row.sides = row.sides_qty = None
		return
	choice = _loads(row.sides, row.item_name)
	changed = bool(choice) and (not old or _loads(old.sides, row.item_name) != choice)
	if changed and not enabled():
		frappe.throw(_("Changing the sides served is off in Restaurant Settings"))
	choice = clean_choice(row.item_code, choice, row.item_name, strict=changed)
	if is_default(row.item_code, choice):
		row.sides = row.sides_qty = None
		return
	per_dish = qty_per_dish(row.item_code, choice)
	if changed or not old or abs(flt(old.qty) - flt(row.qty)) > EPS:
		dish_components(row.item_code, per_dish)  # the bundle must agree with the recipe
		if not any(entry.get("set") for entry in choice.values()):
			# A shared choice is shared again in whole units by check_shared.
			_check_whole(row.item_name, per_dish, row.qty)
	row.sides = _dumps(choice)
	row.sides_qty = _dumps(per_dish)


def _check_whole(label, per_dish, qty):
	"""Stock counted in whole units (pieces, cups) must be served in whole units."""
	for item, q in _flat(per_dish).items():
		total = q * flt(qty)
		if _whole(item) and abs(total - round(total)) > 1e-4:
			frappe.throw(
				_("{0}: {1} is counted in whole units, but {2} would be served").format(
					frappe.bold(label), frappe.bold(frappe.get_cached_value("Item", item, "item_name") or item), _num(total)
				)
			)


def line_choice(row):
	return _loads(row.sides, row.item_name) or default_choice(row.item_code)


def _members(order):
	"""{(set id, slot key): [row]} of the lines of the order that share a choice."""
	out = defaultdict(list)
	for d in order.items:
		if d.kitchen_status == VOID:
			continue
		for key, entry in (_loads(d.sides, d.item_name) or {}).items():
			if entry.get("set"):
				out[(entry["set"], key)].append(d)
	return out


def check_shared(order):
	"""A shared choice whose lines changed (one removed, voided or left it, or a quantity changed).

	The kitchen plated the set's total with its dishes, so when a line is removed, voided uncooked or
	changes quantity, the same total is shared again over the lines still in it (sent ones too: the
	stock follows what was served). When a line left by its own new choice, or was voided after
	cooking (its share is booked as waste), the others keep their own shares."""
	keep = set(order.flags.get("resto_keep_sets") or ())
	for (set_id, key), rows in _members(order).items():
		# A sent line whose recipe lost this side since keeps what it was served with.
		rows = [d for d in rows if key in slots(d.item_code)]
		if not rows:
			continue
		entries = {d.name: _loads(d.sides)[key] for d in rows}
		stale = any(cint(e.get("n")) != len(rows) or abs(flt(e.get("q")) - flt(d.qty)) > EPS for d, e in ((d, entries[d.name]) for d in rows))
		if not stale:
			continue
		if set_id in keep:
			totals = defaultdict(float)
			for d in rows:
				for item, portions in (entries[d.name].get("items") or {}).items():
					totals[item] += flt(portions) * flt(d.qty)
			shares = {d.name: {"items": dict(entries[d.name].get("items") or {})} for d in rows}
			if len(rows) > 1:
				for d in rows:
					shares[d.name].update(set=set_id, n=len(rows), q=flt(d.qty), t={k: flt(v, 9) for k, v in totals.items()})
		else:
			shares = _share(rows, key, entries[rows[0].name].get("t") or {}, set_id, strict=False)
			frappe.msgprint(
				_("The shared {0} was shared again over {1}").format(
					frappe.bold(_slot_label(rows[0].item_code, key)), ", ".join(d.item_name for d in rows)
				),
				alert=True,
				indicator="orange",
			)
		for d in rows:
			choice = _loads(d.sides)
			choice[key] = shares[d.name]
			choice = clean_choice(d.item_code, choice, d.item_name, strict=False)
			if is_default(d.item_code, choice):
				d.sides = d.sides_qty = None
			else:
				d.sides = _dumps(choice)
				d.sides_qty = _dumps(qty_per_dish(d.item_code, choice))


def _share(rows, key, totals, set_id, strict=True):
	"""Share totals {item: portions} over rows, by their recipe's portions of the slot (else by
	quantity). Each row's part is whole in stock units: thousandths, or whole units for stock
	counted in pieces, the remainders going to the largest parts, so the parts add up exactly."""
	precision = frappe.get_precision("Packed Item", "qty") or 3
	weights = [sum(slots(d.item_code)[key].default.values()) * flt(d.qty) for d in rows]
	if sum(weights) <= EPS:
		weights = [flt(d.qty) for d in rows]
	whole = sum(weights)
	per_dish = {d.name: {} for d in rows}
	for item, portions in totals.items():
		portions = flt(portions)
		if portions <= EPS:
			continue
		step = 1 if _whole(item) else 10**-precision
		exact = [portions * w / whole * flt(slots(d.item_code)[key].portion.get(item)) for d, w in zip(rows, weights)]
		units = sum(exact) / step
		if step == 1 and abs(units - round(units)) > 1e-6:
			if strict:
				frappe.throw(
					_("{0} is counted in whole units: choose a whole number of it for these dishes").format(
						frappe.bold(frappe.get_cached_value("Item", item, "item_name") or item)
					)
				)
		parts = [math.floor(v / step + 1e-9) for v in exact]
		left = int(round(units)) - sum(parts)
		for i in sorted(range(len(rows)), key=lambda i: exact[i] / step - parts[i], reverse=True)[: max(left, 0)]:
			parts[i] += 1
		for d, part in zip(rows, parts):
			portion = flt(slots(d.item_code)[key].portion.get(item))
			if part and portion > EPS:
				per_dish[d.name][item] = flt(part * step / portion / flt(d.qty), 9)
	out = {}
	for d in rows:
		entry = {"items": per_dish[d.name]}
		if len(rows) > 1:
			entry.update(set=set_id, n=len(rows), q=flt(d.qty), t={k: flt(v, 9) for k, v in totals.items() if flt(v) > EPS})
		out[d.name] = entry
	return out


def set_order_texts(order):
	"""The "Sides:" text of each unsent line whose sides differ from its recipe; a shared choice is
	written once, on its first line. A sent line keeps the text its ticket was printed with."""
	sets = _members(order)
	sep = _sep()
	for d in order.items:
		if d.kitchen_status != NOT_SENT:
			continue
		all_slots = slots(d.item_code)
		choice = _loads(d.sides, d.item_name)
		if not all_slots or not choice:
			d.sides_text = None
			continue
		served, without, shared = [], [], []
		for key, slot in all_slots.items():
			entry = choice.get(key) or {"items": dict(slot.default)}
			rows = sets.get((entry.get("set"), key)) if entry.get("set") else None
			if rows and len(rows) > 1:
				first = min(rows, key=lambda r: r.idx)
				others = sep.join(r.item_name for r in sorted(rows, key=lambda r: r.idx) if r is not d)
				if first is not d:
					shared.append(_t("{0}: shared with {1}", slot.label, first.item_name))
					continue
				totals = entry.get("t") or {}
				what = sep.join(f"{slot.names.get(i, i)} ×{_num(q)}" for i, q in totals.items() if flt(q) > EPS)
				shared.append(_t("{0} (shared with {1})", what, others) if what else _t("without {0}", slot.label))
				continue
			items = {i: p for i, p in (entry.get("items") or {}).items() if flt(p) > EPS}
			if not items:
				without.append(slot.label)
				continue
			served.extend(f"{slot.names.get(i, i)}" + (f" ×{_num(p)}" if abs(flt(p) - 1) > 1e-6 else "") for i, p in items.items())
		parts = []
		if served:
			parts.append(sep.join(served) + (" " + _t("(per dish)") if flt(d.qty) > 1 else ""))
		if without:
			parts.append(_t("without: {0}", sep.join(without)))
		parts.extend(shared)
		d.sides_text = _t("Sides: {0}", " | ".join(parts)) if parts else None


def combined(order, lines):
	"""Slots of several unsent lines together, with the portions now served in total."""
	rows = _unsent_rows(order, lines)
	out = {}
	for d in rows:
		choice = line_choice(d)
		for key, slot in slots(d.item_code).items():
			c = out.setdefault(key, {"key": key, "label": slot.label, "max": 0, "options": {}})
			c["max"] += slot.max * flt(d.qty)
			for item in slot.portion:
				c["options"].setdefault(item, {"item": item, "item_name": slot.names.get(item, item), "total": 0})
			for item, portions in (choice.get(key, {}).get("items") or {}).items():
				if item in c["options"]:
					c["options"][item]["total"] += flt(portions) * flt(d.qty)
	for c in out.values():
		for o in c["options"].values():
			o["total"] = flt(o["total"], 6)
		c["options"] = list(c["options"].values())
	return list(out.values())


def _unsent_rows(order, lines):
	if isinstance(lines, str):
		lines = json.loads(lines or "[]")
	rows = []
	for name in lines or []:
		row = next((d for d in order.items if d.name == name), None)
		if not row:
			frappe.throw(_("A selected line is not on this order"))
		if row.kitchen_status != NOT_SENT:
			frappe.throw(_("{0} was sent to the kitchen; its sides cannot change").format(frappe.bold(row.item_name)))
		if not slots(row.item_code):
			frappe.throw(_("{0} has no sides in its recipe").format(frappe.bold(row.item_name)))
		rows.append(row)
	if not rows:
		frappe.throw(_("Choose at least one dish"))
	return rows


def _portions(value, label):
	"""{item: portions} from the screen."""
	if not isinstance(value, dict):
		frappe.throw(_("{0}: the sides could not be read").format(frappe.bold(label)))
	if any(flt(p) < -EPS for p in value.values()):
		frappe.throw(_("{0}: portions cannot be negative").format(frappe.bold(label)))
	return {item: flt(p, 9) for item, p in value.items() if flt(p) > EPS}


def _keep_sets(order, rows):
	"""Sets the given lines leave by a new choice of their own: the other lines keep their shares."""
	keep = set(order.flags.get("resto_keep_sets") or ())
	for d in rows:
		for entry in (_loads(d.sides) or {}).values():
			if entry.get("set"):
				keep.add(entry["set"])
	order.flags.resto_keep_sets = keep


def set_for_line(order, line, choice):
	"""Sides of one line, per dish: {slot key: {item: portions}}. Slots left as they are keep
	their choice, a shared one too."""
	if not enabled():
		frappe.throw(_("Changing the sides served is off in Restaurant Settings"))
	(row,) = _unsent_rows(order, [line])
	if not isinstance(choice, dict):
		frappe.throw(_("{0}: the sides could not be read").format(frappe.bold(row.item_name)))
	current = line_choice(row)
	changed = False
	for key, items in choice.items():
		items = _portions(items, row.item_name)
		if _same(items, (current.get(key) or {}).get("items")):
			continue
		if (current.get(key) or {}).get("set"):
			_keep_sets(order, [row])
		current[key] = {"items": items}  # a line of its own leaves a shared choice
		changed = True
	if changed:
		row.sides = _dumps(clean_choice(row.item_code, current, row.item_name))


def set_for_lines(order, lines, totals):
	"""Sides of several lines together: totals {key: {item: portions}} for all of them, shared out
	over the lines that have the slot. A slot whose totals did not change keeps its choice."""
	if not enabled():
		frappe.throw(_("Changing the sides served is off in Restaurant Settings"))
	rows = _unsent_rows(order, lines)
	if not isinstance(totals, dict):
		frappe.throw(_("The sides could not be read"))
	now = {c["key"]: {o["item"]: o["total"] for o in c["options"]} for c in combined(order, [d.name for d in rows])}
	choices = {d.name: line_choice(d) for d in rows}
	for key, items in totals.items():
		having = [d for d in rows if key in slots(d.item_code)]
		if not having:
			frappe.throw(_("None of the chosen dishes has {0}").format(frappe.bold(_slot_label(rows[0].item_code, key))))
		label = slots(having[0].item_code)[key].label
		items = _portions(items, label)
		if _same(items, now.get(key)):
			continue
		limit = sum(slots(d.item_code)[key].max * flt(d.qty) for d in having)
		if sum(items.values()) > limit + 1e-6:
			frappe.throw(_("{0}: the most portions for these dishes is {1}").format(frappe.bold(label), _num(limit)))
		for d in having:
			if (choices[d.name].get(key) or {}).get("set"):
				_keep_sets(order, [d])
		if not items:
			shares = {d.name: {"items": {}} for d in having}  # nothing to share: each is without it
		else:
			shares = _share(having, key, items, frappe.generate_hash(length=8))
		for d in having:
			choices[d.name][key] = shares[d.name]
	for d in rows:
		d.sides = _dumps(clean_choice(d.item_code, choices[d.name], d.item_name))


# --------------------------------------------------------------------------- what a dish deducts


def _flat(qty_by_item):
	"""Stock items of the sides: a side that is a menu dish brings its main ingredients."""
	from resto.resto.doctype.resto_recipe.resto_recipe import get_flat_components

	out = {}
	for item, qty in qty_by_item.items():
		recipe = _menu_recipe(item)
		if recipe:
			get_flat_components(recipe, include_sides=False, multiplier=qty, out=out)
		elif frappe.get_cached_value("Item", item, "is_stock_item"):
			out[item] = out.get(item, 0) + qty
		else:
			frappe.throw(
				_("{0} is not a stock item and has no active menu dish recipe, so it cannot be served as a side").format(
					frappe.bold(frappe.get_cached_value("Item", item, "item_name") or item)
				)
			)
	return out


def dish_components(item_code, sides_qty):
	"""Stock qty per dish: the recipe's main ingredients plus the sides actually served.

	Refused when the dish's Product Bundle is not its recipe (main ingredients plus recipe sides):
	the bundle is what ERPNext deducts for an unchanged dish, so both must agree."""
	from resto.resto.doctype.resto_recipe.resto_recipe import get_flat_components

	name = frappe.get_cached_value("Item", item_code, "item_name") or item_code
	recipe = _menu_recipe(item_code)
	if not recipe:
		frappe.throw(_("{0} has no active recipe, so its sides cannot be changed").format(frappe.bold(name)))
	full = get_flat_components(recipe)
	bundle = defaultdict(float)
	for r in frappe.get_all(
		"Product Bundle Item",
		filters={"parenttype": "Product Bundle", "parent": ("in", frappe.get_all(
			"Product Bundle", filters={"new_item_code": item_code, "disabled": 0}, pluck="name") or [""])},
		fields=["item_code", "qty", "uom"],
	):
		if r.uom and r.uom != frappe.get_cached_value("Item", r.item_code, "stock_uom"):
			bundle = None
			break
		bundle[r.item_code] += flt(r.qty)
	if bundle is None or set(bundle) != set(full) or any(abs(bundle[k] - full[k]) > 1e-6 for k in full):
		frappe.throw(
			_("The stock that {0} deducts differs from its recipe; ask the manager to save the recipe of {0} again").format(
				frappe.bold(name)
			)
		)
	target = defaultdict(float, get_flat_components(recipe, include_sides=False))
	for item, qty in _flat(sides_qty).items():
		target[item] += qty
	return {k: v for k, v in target.items() if v > EPS}


# --------------------------------------------------------------------------- the invoice


def _precision():
	return frappe.get_precision("Packed Item", "qty") or 3


def _check_packed_whole(row, target):
	for item, t in target.items():
		if _whole(item) and abs(flt(t["qty"]) - round(flt(t["qty"]))) > 1e-6:
			frappe.throw(
				_("{0}: {1} is counted in whole units, so bill (or return) all of this dish line together").format(
					frappe.bold(row.item_name or row.item_code), frappe.bold(frappe.get_cached_value("Item", item, "item_name") or item)
				),
				title=_("Sides Check"),
			)


def _target_rows(doc):
	"""{invoice row name: {item: {"qty": ..., extra fields}}} for every row Resto decides; rows not
	listed keep what ERPNext packed from the bundle."""
	prec = _precision()
	out = {}
	for row in doc.items:
		if doc.get("is_return"):
			original = row.get("sales_invoice_item")
			if not original:
				if doc.get("return_against") and frappe.db.exists(
					"Sales Invoice Item", {"parent": doc.return_against, "item_code": row.item_code, "resto_sides": ("is", "set")}
				):
					frappe.throw(
						_("{0} was sold with changed sides: return it from the invoice it was sold on (Create → Return), so what was served comes back").format(
							frappe.bold(row.item_name or row.item_code)
						),
						title=_("Sides Check"),
					)
				continue
			orig = frappe.db.get_value("Sales Invoice Item", original, ["resto_sides", "stock_qty", "parent", "item_code"], as_dict=True)
			if not orig or not orig.resto_sides:
				continue
			if orig.item_code != row.item_code or not flt(orig.stock_qty):
				frappe.throw(_("{0}: the returned line does not match the invoice line it returns").format(frappe.bold(row.item_name)))
			ratio = flt(row.stock_qty) / flt(orig.stock_qty)
			# The return that takes back the rest of the line takes back exactly what is left.
			returned = abs(flt(frappe.db.sql(
				"""select sum(stock_qty) from `tabSales Invoice Item` where sales_invoice_item=%s and docstatus=1 and parent!=%s""",
				(original, doc.name or ""),
			)[0][0]))
			last = abs(flt(row.stock_qty)) + returned >= flt(orig.stock_qty) - 1e-9
			target = {}
			for p in frappe.get_all(
				"Packed Item",
				filters={"parenttype": "Sales Invoice", "parent": orig.parent, "parent_detail_docname": original},
				fields=["name", "item_code", "qty"],
				order_by="idx asc",
			):
				if last:
					back = abs(flt(frappe.db.sql(
						"""select sum(qty) from `tabPacked Item` where return_against_packed_item=%s and docstatus=1 and parent!=%s""",
						(p.name, doc.name or ""),
					)[0][0]))
					qty = -flt(flt(p.qty) - back, prec)
				else:
					qty = flt(flt(p.qty) * ratio, prec)
				t = target.setdefault(p.item_code, {"qty": 0, "return_against_packed_item": p.name})
				t["qty"] = flt(t["qty"] + qty, prec)
			_check_packed_whole(row, target)
			out[row.name] = target
		elif row.get("resto_sides"):
			per_dish = dish_components(row.item_code, _loads(row.resto_sides, row.item_name))
			target = {k: {"qty": flt(v * flt(row.stock_qty), prec)} for k, v in per_dish.items()}
			_check_packed_whole(row, target)
			out[row.name] = target
	return out


def _new_packed_row(doc, row, item_code, template):
	item = frappe.get_cached_value("Item", item_code, ["item_name", "stock_uom", "description", "valuation_rate"], as_dict=True)
	warehouse = (template.warehouse if template else None) or row.warehouse
	p = doc.append("packed_items", {})
	p.update(
		{
			"parent_item": row.item_code,
			"parent_detail_docname": row.name or row.idx,
			"item_code": item_code,
			"item_name": item.item_name,
			"uom": item.stock_uom,
			"conversion_factor": row.conversion_factor,
			"description": item.description,
			"warehouse": warehouse,
			"target_warehouse": row.get("target_warehouse"),
			"rate": flt(item.valuation_rate),
			"actual_qty": flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")),
			"use_serial_batch_fields": frappe.db.get_single_value("Stock Settings", "use_serial_batch_fields"),
		}
	)
	return p


def _guard_resto_sides(doc):
	"""Sides Served is written by the floor screen's billing only, never typed on an invoice."""
	if doc.get("is_return") or doc.flags.get("resto_billing"):
		return
	before = doc.get_doc_before_save()
	old = {r.name: r.get("resto_sides") for r in (before.items if before else [])}
	for row in doc.items:
		if row.get("resto_sides") and old.get(row.name) != row.resto_sides:
			frappe.throw(
				_("{0}: Sides Served is set by the floor screen only").format(frappe.bold(row.item_name or row.item_code)),
				title=_("Sides Check"),
			)


def apply_to_invoice(doc, method=None):
	"""Sales Invoice validate (after ERPNext packed the bundles): set the packed rows of the dish
	rows whose sides were changed, and of returns of such rows."""
	if doc.doctype != "Sales Invoice":
		return
	_guard_resto_sides(doc)
	if not cint(doc.get("update_stock")):
		return
	targets = _target_rows(doc)
	if not targets:
		return
	by_name = {row.name: row for row in doc.items}
	for row_name, target in targets.items():
		row = by_name[row_name]
		mine = [p for p in doc.packed_items if p.parent_detail_docname == row.name and p.parent_item == row.item_code]
		template = mine[0] if mine else None
		keep = []
		for item_code, t in target.items():
			if abs(flt(t["qty"])) <= EPS:
				continue
			p = next((x for x in mine if x.item_code == item_code and x not in keep), None) or _new_packed_row(doc, row, item_code, template)
			p.qty = t["qty"]
			if "return_against_packed_item" in t:
				p.return_against_packed_item = t["return_against_packed_item"]
			keep.append(p)
		for p in mine:
			if p not in keep:
				doc.remove(p)
	for i, p in enumerate(doc.packed_items, 1):
		p.idx = i


def check_invoice(doc, method=None):
	"""Sales Invoice before submit: every dish row Resto decided must pack exactly what it should,
	and ERPNext must post every one of those rows. Anything else stops the bill."""
	if doc.doctype != "Sales Invoice" or not cint(doc.get("update_stock")):
		return
	targets = _target_rows(doc)
	if not targets:
		return
	tolerance = 10 ** -_precision()
	by_name = {row.name: row for row in doc.items}
	for row_name, target in targets.items():
		row = by_name[row_name]
		label = frappe.bold(row.item_name or row.item_code)
		if not doc.has_product_bundle(row.item_code):
			frappe.throw(_("{0} has no active recipe bundle, so its stock would not be posted").format(label), title=_("Sides Check"))
		sides = set() if doc.get("is_return") else set(_flat(_loads(row.resto_sides, row.item_name) or {}))
		packed = defaultdict(float)
		for p in doc.packed_items:
			if p.parent_detail_docname != row.name or p.parent_item != row.item_code:
				continue
			if not frappe.get_cached_value("Item", p.item_code, "is_stock_item"):
				frappe.throw(_("{0}: {1} is not a stock item, so it would not be deducted").format(label, p.item_code), title=_("Sides Check"))
			if (doc.get("is_return") or p.item_code in sides) and (
				frappe.get_cached_value("Item", p.item_code, "has_serial_no") or frappe.get_cached_value("Item", p.item_code, "has_batch_no")
			):
				frappe.throw(_("{0}: changed sides cannot use serial or batch items ({1})").format(label, p.item_code), title=_("Sides Check"))
			packed[p.item_code] += flt(p.qty)
		expected = {k: flt(t["qty"]) for k, t in target.items() if abs(flt(t["qty"])) > EPS}
		if set(packed) != set(expected) or any(abs(packed[k] - expected[k]) > tolerance for k in expected):
			frappe.throw(
				_("{0}: the stock this dish deducts is not what its sides say. Nothing was posted; tell the manager.").format(label),
				title=_("Sides Check"),
			)
