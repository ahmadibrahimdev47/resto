import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

from resto.resto.utils import get_settings


class RestoWaste(Document):
	def validate(self):
		if self.is_new() and self.amended_from and self.order_item:
			# Its dish is free again: posted from the order, the shares of a shared side come right.
			frappe.throw(_("This waste was posted for a voided dish; post it again from its order instead of amending it"))
		settings = get_settings()
		self.company = self.company or settings.company
		self.warehouse = self.warehouse or settings.kitchen_warehouse
		self.waste_account = self.waste_account or settings.waste_account
		if self.warehouse and frappe.db.get_value("Warehouse", self.warehouse, "company") != self.company:
			frappe.throw(_("Warehouse {0} does not belong to company {1}").format(frappe.bold(self.warehouse), self.company))
		if self.waste_account:
			company, is_group = frappe.db.get_value("Account", self.waste_account, ["company", "is_group"])
			if company != self.company or is_group:
				frappe.throw(_("Choose a non-group account of company {0}").format(self.company))

		total = 0
		for row in self.items:
			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: quantity must be more than zero").format(row.idx))
			item = frappe.get_cached_value("Item", row.item_code, ["item_name", "stock_uom", "is_stock_item"], as_dict=True)
			row.item_name = item.item_name
			row.uom = item.stock_uom
			if not item.is_stock_item and not _bundle(row.item_code):
				frappe.throw(_("Row {0}: {1} is not a stock item and has no recipe bundle").format(row.idx, frappe.bold(row.item_code)))
			if self.docstatus == 0:
				# An estimate; the posted cost replaces it on submit.
				row.valuation_rate = _estimated_rate(row.item_code, self.warehouse)
				row.amount = flt(row.valuation_rate * flt(row.qty), row.precision("amount"))
			total += flt(row.amount)
		self.total_amount = total

	def on_submit(self):
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Issue"
		se.purpose = "Material Issue"
		se.company = self.company
		se.from_warehouse = self.warehouse
		se.posting_date = self.posting_date
		se.set_posting_time = 1 if getdate(self.posting_date) != getdate(nowdate()) else 0
		se.remarks = _("Food waste {0}").format(self.name)
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")
		owner_row = []  # waste row index for each stock entry row
		for row in self.items:
			for item_code, qty in _components(row.item_code, flt(row.qty)):
				uom = frappe.get_cached_value("Item", item_code, "stock_uom")
				se.append(
					"items",
					{
						"item_code": item_code,
						"qty": qty,
						"uom": uom,
						"stock_uom": uom,
						"conversion_factor": 1,
						"s_warehouse": self.warehouse,
						"expense_account": self.waste_account,
						"cost_center": cost_center,
					},
				)
				owner_row.append(row)
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

		# Record the cost actually posted by the stock ledger (not the rows' estimate).
		posted = dict(
			frappe.db.sql(
				"""select voucher_detail_no, sum(-stock_value_difference) from `tabStock Ledger Entry`
				where voucher_type = 'Stock Entry' and voucher_no = %s and is_cancelled = 0 group by voucher_detail_no""",
				se.name,
			)
		)
		cost = {}
		for se_row, row in zip(se.items, owner_row, strict=True):
			cost[row.name] = cost.get(row.name, 0) + flt(posted.get(se_row.name))
		total = 0
		for row in self.items:
			amount = flt(cost.get(row.name), row.precision("amount"))
			row.db_set({"amount": amount, "valuation_rate": flt(amount / flt(row.qty)) if flt(row.qty) else 0})
			total += amount
		self.db_set({"stock_entry": se.name, "total_amount": total})

	def on_cancel(self):
		if self.stock_entry and frappe.db.get_value("Stock Entry", self.stock_entry, "docstatus") == 1:
			se = frappe.get_doc("Stock Entry", self.stock_entry)
			se.flags.ignore_permissions = True
			se.cancel()
		# The dish whose waste this was can have it posted again, and the shares of a shared side it
		# took from other dishes go back to them.
		for name in frappe.get_all("Resto Order Item", filters={"void_waste": self.name}, pluck="name"):
			frappe.db.set_value("Resto Order Item", name, "void_waste", None, update_modified=False)
		order = self.order_item and frappe.db.get_value("Resto Order Item", self.order_item, "parent") or self.resto_order
		if order:
			# One at a time with a waste being posted on the order.
			frappe.db.get_value("Resto Order", order, "name", for_update=True)
			for d in frappe.get_all(
				"Resto Order Item", filters={"parent": order, "sides": ("like", f"%{self.name}%")}, fields=["name", "sides"]
			):
				choice = json.loads(d.sides)
				marked = [e for e in choice.values() if isinstance(e, dict) and e.get("w") == self.name]
				for e in marked:
					e.pop("w")
				if marked:
					frappe.db.set_value(
						"Resto Order Item", d.name, "sides", json.dumps(choice, ensure_ascii=False, sort_keys=True), update_modified=False
					)


def _bundle(item_code):
	return frappe.db.get_value("Product Bundle", {"new_item_code": item_code, "disabled": 0}, "name")


def _components(item_code, qty, depth=0):
	"""A stock item as is; a menu dish (Product Bundle) as its components."""
	if frappe.get_cached_value("Item", item_code, "is_stock_item"):
		return [(item_code, qty)]
	bundle = _bundle(item_code)
	if not bundle or depth > 5:
		frappe.throw(_("{0} is not a stock item").format(frappe.bold(item_code)))
	result = []
	for it in frappe.get_doc("Product Bundle", bundle).items:
		result += _components(it.item_code, qty * flt(it.qty), depth + 1)
	return result


def _estimated_rate(item_code, warehouse):
	total = 0
	for code, qty in _components(item_code, 1):
		rate = flt(frappe.db.get_value("Bin", {"item_code": code, "warehouse": warehouse}, "valuation_rate")) or flt(
			frappe.get_cached_value("Item", code, "valuation_rate")
		)
		total += rate * qty
	return total
