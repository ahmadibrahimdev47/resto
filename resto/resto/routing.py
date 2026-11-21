"""Which kitchen station makes an item: item list, then item group up the tree, then default."""

import frappe

CACHE_KEY = "resto_station_routing"


def get_routing():
	routing = frappe.cache.get_value(CACHE_KEY)
	if routing is None:
		routing = _build_routing()
		frappe.cache.set_value(CACHE_KEY, routing)
	return routing


def _build_routing():
	routing = {"items": {}, "groups": {}, "default": None}
	stations = frappe.get_all(
		"Resto Kitchen Station", filters={"is_active": 1}, fields=["name", "is_default"], order_by="name"
	)
	active = {s.name for s in stations}
	for s in stations:
		if s.is_default and not routing["default"]:
			routing["default"] = s.name
	for row in frappe.get_all(
		"Resto Station Item", filters={"parenttype": "Resto Kitchen Station"}, fields=["parent", "item_code"]
	):
		if row.parent in active:
			routing["items"].setdefault(row.item_code, row.parent)
	for row in frappe.get_all(
		"Resto Station Item Group",
		filters={"parenttype": "Resto Kitchen Station"},
		fields=["parent", "item_group"],
	):
		if row.parent in active:
			routing["groups"].setdefault(row.item_group, row.parent)
	return routing


def clear_cache(doc=None, method=None):
	frappe.cache.delete_value(CACHE_KEY)


def get_station(item_code):
	routing = get_routing()
	if item_code in routing["items"]:
		return routing["items"][item_code]

	group = frappe.get_cached_value("Item", item_code, "item_group")
	seen = set()
	while group and group not in seen:
		if group in routing["groups"]:
			return routing["groups"][group]
		seen.add(group)
		group = frappe.get_cached_value("Item Group", group, "parent_item_group")

	return routing["default"]
