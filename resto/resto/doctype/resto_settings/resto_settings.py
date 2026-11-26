import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class RestoSettings(Document):
	def validate(self):
		for field in ("main_warehouse", "kitchen_warehouse"):
			warehouse = self.get(field)
			if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") != self.company:
				frappe.throw(
					_("Warehouse {0} does not belong to company {1}").format(
						frappe.bold(warehouse), frappe.bold(self.company)
					)
				)

		if self.waste_account:
			company, is_group = frappe.db.get_value(
				"Account", self.waste_account, ["company", "is_group"]
			) or (None, None)
			if company != self.company:
				frappe.throw(
					_("Account {0} does not belong to company {1}").format(
						frappe.bold(self.waste_account), frappe.bold(self.company)
					)
				)
			if is_group:
				frappe.throw(_("Account {0} is a group account").format(frappe.bold(self.waste_account)))

		if self.get("staff_meal_account"):
			company, is_group, root_type = frappe.db.get_value(
				"Account", self.staff_meal_account, ["company", "is_group", "root_type"]
			) or (None, None, None)
			if company != self.company or is_group or root_type != "Expense":
				frappe.throw(
					_("The Staff Meals Account {0} must be an expense account of company {1}, not a group").format(
						frappe.bold(self.staff_meal_account), frappe.bold(self.company)
					)
				)

		if self.get("staff_advance_account"):
			from resto.resto.staff_advance import account_problem

			problem = account_problem(self.staff_advance_account, self.company)
			if problem:
				frappe.throw(problem)
		if flt(self.get("staff_advance_cap")) < 0:
			frappe.throw(_("Staff Advance Needs a Manager Above cannot be negative"))

		from resto.resto.drawer_cash import main_safe_problem, other_problem

		if self.get("main_safe_account"):
			problem = main_safe_problem(self.main_safe_account, self.company)
			if problem:
				frappe.throw(problem)
		for row in self.get("drawer_cash_in_accounts") or []:
			problem = other_problem(row.account, self.company)
			if problem:
				frappe.throw(problem)
			if row.account in (self.get("main_safe_account"), self.get("staff_advance_account")):
				frappe.throw(
					_("{0} has its own way into the drawer (change from the main safe, or a staff advance paid back)").format(
						frappe.bold(row.account)
					)
				)

		for field in ("cash_over_short_account",):
			account = self.get(field)
			if not account:
				continue
			company, is_group, root_type = frappe.db.get_value("Account", account, ["company", "is_group", "root_type"]) or (
				None,
				None,
				None,
			)
			if root_type not in ("Expense", "Income"):
				frappe.throw(_("Account {0} must be an expense or income account").format(frappe.bold(account)))
			if company != self.company:
				frappe.throw(
					_("Account {0} does not belong to company {1}").format(frappe.bold(account), frappe.bold(self.company))
				)
			if is_group:
				frappe.throw(_("Account {0} is a group account").format(frappe.bold(account)))
		for row in self.get("drawer_expense_accounts") or []:
			if frappe.db.get_value("Account", row.account, "root_type") != "Expense":
				frappe.throw(_("{0} is not an expense account").format(frappe.bold(row.account)))

		self.validate_food_cost_levels()
		self.validate_consumables()
		if not self.cashier_print_method:
			self.cashier_print_method = "Server Printer" if self.cashier_printer else "Browser Button"
		if self.cashier_print_method == "Server Printer" and not self.cashier_printer:
			frappe.throw(_("Choose the cashier printer, or another printing method"))

	def validate_consumables(self):
		"""Automatic Consumables (D-224): each billed as its own stock line at no price, once per order
		or per guest, on the order types ticked."""
		from resto.resto.doctype.resto_recipe.resto_recipe import MENU, get_active_recipe
		from resto.resto.packaging import TYPE_FLAG, item_problem

		seen = set()
		for row in self.get("consumables") or []:
			label = _("Row {0} in {1}").format(row.idx, _("Automatic Consumables"))
			if get_active_recipe(row.item_code, MENU):
				frappe.throw(_("{0}: {1} must be a stock item, not a menu dish").format(label, frappe.bold(row.item_code)))
			problem = item_problem(row.item_code)
			if problem:
				frappe.throw(f"{label}: {problem}")
			if flt(row.qty) <= 0:
				frappe.throw(_("{0}: quantity must be greater than zero").format(label))
			if not any(cint(row.get(f)) for f in TYPE_FLAG.values()):
				frappe.throw(_("{0}: tick at least one order type that takes {1}").format(label, frappe.bold(row.item_code)))
			if row.item_code in seen:
				frappe.throw(_("{0}: {1} is listed twice").format(label, frappe.bold(row.item_code)))
			seen.add(row.item_code)

	def validate_food_cost_levels(self):
		from resto.resto.food_cost import STEP_FIELDS, STEPS

		for key, field in STEP_FIELDS.items():
			if not flt(self.get(field)):
				self.set(field, STEPS[key])
		if not flt(self.food_cost_yellow_step) < flt(self.food_cost_orange_step):
			frappe.throw(_("Orange points above target must be more than Yellow points above target."))


def get_settings():
	"""Return Resto Settings, with company falling back to the default company."""
	settings = frappe.get_cached_doc("Resto Settings")
	if not settings.company:
		settings.company = frappe.defaults.get_global_default("company")
	return settings
