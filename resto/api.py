import frappe
from frappe import _

from resto.resto.doctype.resto_recipe.resto_recipe import MENU, PREPARED
from resto.resto.doctype.resto_settings.resto_settings import get_settings

MANAGER_ROLES = ("System Manager", "Resto Manager", "Manufacturing Manager", "Stock Manager")


@frappe.whitelist(methods=["POST"])
def import_existing_recipes():
	"""Create a recipe for each default BOM and each enabled Product Bundle that has none yet.

	An imported prepared recipe keeps pointing at its existing BOM, so no new BOM is made.
	"""
	frappe.only_for(MANAGER_ROLES)
	company = get_settings().company
	result = {"created": [], "skipped": [], "failed": []}

	boms = frappe.get_all(
		"BOM",
		filters={"docstatus": 1, "is_active": 1, "is_default": 1, "company": company},
		fields=["name", "item"],
		order_by="item asc",
	)
	for row in boms:
		if frappe.db.exists("Resto Recipe", {"item": row.item}):
			result["skipped"].append(row.item)
			continue
		bom = frappe.get_doc("BOM", row.name)
		recipe = frappe.new_doc("Resto Recipe")
		recipe.update(
			{
				"recipe_type": PREPARED,
				"item": bom.item,
				"company": bom.company,
				"quantity": bom.quantity,
				"bom": bom.name,
			}
		)
		for it in bom.items:
			recipe.append("items", {"item_code": it.item_code, "qty": it.qty, "uom": it.uom})
		_insert(recipe, result)

	bundles = frappe.get_all(
		"Product Bundle", filters={"disabled": 0}, fields=["name", "new_item_code"], order_by="name asc"
	)
	for row in bundles:
		if frappe.db.exists("Resto Recipe", {"item": row.new_item_code}):
			result["skipped"].append(row.new_item_code)
			continue
		bundle = frappe.get_doc("Product Bundle", row.name)
		recipe = frappe.new_doc("Resto Recipe")
		recipe.update({"recipe_type": MENU, "item": bundle.new_item_code, "company": company})
		for it in bundle.items:
			recipe.append("items", {"item_code": it.item_code, "qty": it.qty, "uom": it.uom})
		_insert(recipe, result)

	return result


def _insert(recipe, result):
	frappe.db.savepoint("resto_import")
	try:
		recipe.insert()
		result["created"].append(recipe.item)
	except Exception as e:
		frappe.db.rollback(save_point="resto_import")
		frappe.clear_messages()
		result["failed"].append(f"{recipe.item}: {frappe.utils.strip_html(str(e))}")


@frappe.whitelist(methods=["POST"])
def update_all_recipe_costs():
	"""Recalculate stored costs of all active recipes. Costs are computed live and recursively,
	so the order does not matter."""
	frappe.only_for(MANAGER_ROLES)
	return refresh_recipe_costs()


def refresh_recipe_costs():
	"""Daily job and button: store current costs on every active recipe."""
	names = frappe.get_all("Resto Recipe", filters={"is_active": 1}, pluck="name")
	for name in names:
		frappe.get_doc("Resto Recipe", name).update_cost_fields()
	return len(names)
