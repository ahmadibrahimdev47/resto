"""Test data for a throw-away test site. Never run against a real site."""

import frappe
from frappe.utils import flt

COMPANY = "Resto Test Co"
ABBR = "RTC"
MAIN = f"Main Store - {ABBR}"
KITCHEN = f"Kitchen - {ABBR}"
PRICE_LIST = "Standard Selling"
PROFILE = "Resto Test POS"
CUSTOMER = "Walk In Guest"


def guard():
	if frappe.local.site in ("erp.local",) or not frappe.conf.get("allow_tests"):
		raise RuntimeError("Resto test fixtures run only on a test site with allow_tests")


def ensure_setup():
	guard()
	if not frappe.db.get_single_value("System Settings", "setup_complete"):
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		setup_complete(
			{
				"language": "English",
				"country": "Egypt",
				"timezone": "Africa/Cairo",
				"currency": "EGP",
				"company_name": COMPANY,
				"company_abbr": ABBR,
				"chart_of_accounts": "Standard",
				"fy_start_date": "2026-01-01",
				"fy_end_date": "2026-12-31",
				"full_name": "Test Admin",
				"email": "resto.admin@example.com",
				"password": frappe.generate_hash(length=16),
			}
		)
		frappe.db.commit()
	frappe.set_user("Administrator")
	make_users()  # also on a site set up before a user was added
	if frappe.db.exists("Resto Hall", "Main Hall"):
		frappe.db.commit()
		return
	make_masters()
	frappe.db.commit()


def make_users():
	for user, role in (
		("waiter@resto.test", "Resto Waiter"),
		("cashier@resto.test", "Resto Cashier"),
		("kitchen@resto.test", "Resto Kitchen"),
	):
		if not frappe.db.exists("User", user):
			u = frappe.get_doc({"doctype": "User", "email": user, "first_name": role, "send_welcome_email": 0})
			u.insert(ignore_permissions=True)
			u.add_roles(role)


def doc(doctype, name=None, **values):
	if name and frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)
	d = frappe.get_doc({"doctype": doctype, **values})
	d.insert(ignore_permissions=True)
	return d


def account(name, parent, root_type=None, account_type=None, is_group=0):
	full = f"{name} - {ABBR}"
	if not frappe.db.exists("Account", full):
		doc(
			"Account",
			account_name=name,
			parent_account=parent,
			company=COMPANY,
			account_type=account_type,
			is_group=is_group,
		)
	return full


def item(code, uom="Nos", stock=1, group="All Item Groups", valuation=0, sales=1):
	return doc(
		"Item",
		code,
		item_code=code,
		item_name=code.replace("-", " ").title(),
		item_group=group,
		stock_uom=uom,
		is_stock_item=stock,
		is_sales_item=sales,
		include_item_in_manufacturing=1,
		valuation_rate=valuation,
	)


def price(code, rate):
	if not frappe.db.exists("Item Price", {"item_code": code, "price_list": PRICE_LIST}):
		doc("Item Price", item_code=code, price_list=PRICE_LIST, price_list_rate=rate)


def receive(warehouse, rows):
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": COMPANY,
			"to_warehouse": warehouse,
			"items": [
				{"item_code": c, "qty": q, "basic_rate": r, "t_warehouse": warehouse, "conversion_factor": 1}
				for c, q, r in rows
			],
		}
	)
	se.insert(ignore_permissions=True)
	se.submit()
	return se


def make_masters():
	for g, parent in (
		("Resto Menu", "All Item Groups"),
		("Resto Food", "Resto Menu"),
		("Resto Drinks", "Resto Menu"),
		("Resto Raw", "All Item Groups"),
		("Resto Prepared", "All Item Groups"),
	):
		doc("Item Group", g, item_group_name=g, parent_item_group=parent, is_group=1 if g == "Resto Menu" else 0)
	for uom in ("Kg", "Nos"):
		if not frappe.db.exists("UOM", uom):
			doc("UOM", uom, uom_name=uom)
		frappe.db.set_value("UOM", uom, "must_be_whole_number", 0)

	for wh in ("Main Store", "Kitchen"):
		doc("Warehouse", f"{wh} - {ABBR}", warehouse_name=wh, company=COMPANY)

	# Raw materials and stock items.
	item("RAW-BEEF", "Kg", group="Resto Raw", valuation=300)
	item("RAW-BUN", group="Resto Raw", valuation=5)
	item("RAW-CHEESE", "Kg", group="Resto Raw", valuation=200)
	item("RAW-POTATO", "Kg", group="Resto Raw", valuation=20)
	item("DRK-COLA", group="Resto Drinks", valuation=10)
	item("PREP-PATTY", group="Resto Prepared", valuation=45)
	# Menu dishes are non-stock items made of a bundle.
	item("DISH-BURGER", stock=0, group="Resto Food")
	item("DISH-FRIES", stock=0, group="Resto Food")
	item("EXTRA-CHEESE", stock=0, group="Resto Food")
	item("DELIVERY-FEE", stock=0, group="All Item Groups")

	receive(MAIN, [("RAW-BEEF", 50, 300), ("RAW-BUN", 200, 5), ("RAW-CHEESE", 20, 200), ("RAW-POTATO", 100, 20), ("DRK-COLA", 100, 10)])
	receive(KITCHEN, [("RAW-BEEF", 5, 300), ("RAW-BUN", 100, 5), ("RAW-CHEESE", 5, 200), ("RAW-POTATO", 30, 20), ("DRK-COLA", 50, 10)])

	for code, rate in (("DISH-BURGER", 150), ("DISH-FRIES", 40), ("DRK-COLA", 25), ("EXTRA-CHEESE", 15), ("DELIVERY-FEE", 20)):
		price(code, rate)

	# Accounts and taxes.
	duties = frappe.db.get_value("Account", {"company": COMPANY, "account_name": "Duties and Taxes", "is_group": 1})
	service = account("Service Charge", duties, account_type="Tax")
	vat = account("VAT 14", duties, account_type="Tax")
	expenses = frappe.db.get_value("Account", {"company": COMPANY, "account_name": "Indirect Expenses", "is_group": 1})
	waste = account("Food Waste", expenses, account_type="Expense Account")
	cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")
	doc(
		"Sales Taxes and Charges Template",
		f"Resto VAT - {ABBR}",
		title="Resto VAT",
		company=COMPANY,
		taxes=[{"charge_type": "On Net Total", "account_head": vat, "description": "VAT 14%", "rate": 14, "cost_center": cost_center}],
	)
	doc(
		"Sales Taxes and Charges Template",
		f"Resto VAT on Service - {ABBR}",
		title="Resto VAT on Service",
		company=COMPANY,
		taxes=[
			{"charge_type": "On Net Total", "account_head": service, "description": "Service", "rate": 0, "cost_center": cost_center},
			{"charge_type": "On Previous Row Total", "row_id": "1", "account_head": vat, "description": "VAT 14%", "rate": 14, "cost_center": cost_center},
		],
	)

	cash = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Cash", "is_group": 0})
	bank = account("Card Clearing", frappe.db.get_value("Account", {"company": COMPANY, "account_name": "Bank Accounts", "is_group": 1}), account_type="Bank")
	for mode, acc, mtype in (("Cash", cash, "Cash"), ("Card", bank, "Bank")):
		mop = doc("Mode of Payment", mode, mode_of_payment=mode, type=mtype)
		if not any(a.company == COMPANY for a in mop.accounts):
			mop.append("accounts", {"company": COMPANY, "default_account": acc})
			mop.save(ignore_permissions=True)

	doc(
		"Customer",
		CUSTOMER,
		customer_name=CUSTOMER,
		customer_group=frappe.db.get_value("Customer Group", {"is_group": 0}),
		territory=frappe.db.get_value("Territory", {"is_group": 0}),
	)
	doc(
		"POS Profile",
		PROFILE,
		__newname=PROFILE,
		company=COMPANY,
		warehouse=KITCHEN,
		selling_price_list=PRICE_LIST,
		currency="EGP",
		customer=CUSTOMER,
		update_stock=1,
		taxes_and_charges=f"Resto VAT - {ABBR}",
		write_off_account=frappe.db.get_value("Account", {"company": COMPANY, "account_name": "Write Off"}) or waste,
		write_off_cost_center=cost_center,
		cost_center=cost_center,
		payments=[{"mode_of_payment": "Cash", "default": 1}, {"mode_of_payment": "Card"}],
	)

	settings = frappe.get_doc("Resto Settings")
	settings.update(
		{
			"company": COMPANY,
			"selling_price_list": PRICE_LIST,
			"main_warehouse": MAIN,
			"kitchen_warehouse": KITCHEN,
			"prepared_item_group": "Resto Prepared",
			"menu_item_group": "Resto Menu",
			"prepared_allow_negative": 1,
			"waste_account": waste,
			"pos_profile": PROFILE,
			"require_pos_shift": 0,
			"service_charge_percent": 12,
			"service_charge_account": service,
			"service_on_dine_in": 1,
			"service_on_takeaway": 0,
			"service_on_delivery": 0,
			"delivery_fee_item": "DELIVERY-FEE",
			"delivery_fee": 20,
			"waiter_discount_limit": 0,
			"cashier_discount_limit": 10,
		}
	)
	settings.save(ignore_permissions=True)

	# Recipes: a prepared patty (BOM) and menu dishes (bundles).
	recipe("Prepared Ingredient", "PREP-PATTY", [("RAW-BEEF", 0.15)], par_qty=20)
	recipe("Menu Dish", "DISH-BURGER", [("PREP-PATTY", 1), ("RAW-BUN", 1), ("RAW-CHEESE", 0.02)])
	recipe("Menu Dish", "DISH-FRIES", [("RAW-POTATO", 0.25)])
	recipe("Menu Dish", "EXTRA-CHEESE", [("RAW-CHEESE", 0.03)])

	doc("Resto Hall", "Main Hall", hall_name="Main Hall", grid_columns=4)
	for i in range(1, 7):
		doc("Resto Table", f"T{i}", table_name=f"T{i}", hall="Main Hall", seats=4)
	doc("Resto Kitchen Station", "Grill", station_name="Grill", is_default=1, item_groups=[{"item_group": "Resto Food"}])
	doc("Resto Kitchen Station", "Bar", station_name="Bar", item_groups=[{"item_group": "Resto Drinks"}])
	doc(
		"Resto Modifier Group",
		"Doneness",
		group_name="Doneness",
		min_select=1,
		max_select=1,
		options=[{"option_name": "Medium"}, {"option_name": "Well Done"}],
		applies_to=[{"item_code": "DISH-BURGER"}],
	)
	doc(
		"Resto Modifier Group",
		"Extras",
		group_name="Extras",
		options=[{"option_name": "Extra Cheese", "price": 15, "item": "EXTRA-CHEESE"}, {"option_name": "No Onion"}],
		applies_to=[{"item_code": "DISH-BURGER"}],
	)



def recipe(recipe_type, code, rows, par_qty=0):
	if frappe.db.exists("Resto Recipe", code):
		return frappe.get_doc("Resto Recipe", code)
	r = frappe.get_doc(
		{
			"doctype": "Resto Recipe",
			"recipe_type": recipe_type,
			"item": code,
			"company": COMPANY,
			"quantity": 1,
			"par_qty": par_qty,
			"items": [{"item_code": c, "qty": q, "uom": frappe.get_cached_value("Item", c, "stock_uom")} for c, q in rows],
		}
	)
	r.insert(ignore_permissions=True)
	return r


def bin_qty(code, warehouse=KITCHEN):
	return flt(frappe.db.get_value("Bin", {"item_code": code, "warehouse": warehouse}, "actual_qty"))
