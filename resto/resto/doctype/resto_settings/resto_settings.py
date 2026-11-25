import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


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
		if not self.cashier_print_method:
			self.cashier_print_method = "Server Printer" if self.cashier_printer else "Browser Button"
		if self.cashier_print_method == "Server Printer" and not self.cashier_printer:
			frappe.throw(_("Choose the cashier printer, or another printing method"))

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
