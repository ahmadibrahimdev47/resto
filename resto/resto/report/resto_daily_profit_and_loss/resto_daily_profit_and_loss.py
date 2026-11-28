"""Daily profit and loss (D-289): the restaurant's statement for a day or a range of days, read only
from the general ledger, so its net profit is the ledger's income less its expenses for the same
dates (cancelled entries and period closing left out).

Each income and expense entry of the range lands in exactly one line:
- on a sales invoice: income is the sales (a return's, the returns); an expense on an item's cost
  account is the cost of what was sold (packaging apart, from the stock ledger); any other expense
  (round off, write off, a discount account) is a deduction from the sales;
- elsewhere: income is other income; an expense is direct (counted before gross profit) when it sits
  in the same top group of the chart as the company's cost of goods sold account (Direct Expenses,
  whatever the chart calls it), is "Include in Gross" or under a group that is, or is a stock cost
  account; any other expense is indirect.

Gross sales add back the discounts and the taxes billed, so the statement reads from what was
charged down to net sales; those additions are taken from the invoices themselves.

"Spread fixed expenses" shows each account marked fixed in Resto Settings at its month's total
divided by the days of that month, times the days of the range: rent paid on the 1st no longer
makes that one day a loss. The net profit then no longer equals the ledger's, and those lines say so.

The cost of goods sold view lists the items sold with what the stock ledger posted for each.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_first_day, get_last_day

from resto.resto.report_utils import cost_by_invoice_row, excluded_items, invoice_condition, packaging_cost, pct, prepare_filters
from resto.resto.utils import get_settings

CTX = "Resto Daily Profit and Loss"
COGS_VIEW = "Cost of Goods Sold"
# Expense accounts that are a cost of the food by their type, wherever they sit in the chart.
DIRECT_TYPES = ("Cost of Goods Sold", "Stock Adjustment", "Expenses Included In Valuation")


def execute(filters=None):
	filters = prepare_filters(filters)
	if filters.get("view") == COGS_VIEW:
		return cogs_view(filters)
	pnl = statement(filters, spread_fixed=cint(filters.get("spread_fixed")))
	return statement_columns(), pnl.rows, statement_message(pnl), None, summary(pnl)


# ---------------------------------------------------------------------------------- the ledger


def ledger(filters):
	"""Income and expense of the range per account, split by sales invoice (and return) or not."""
	return frappe.db.sql(
		"""
		select gle.account, acc.root_type,
			gle.voucher_type = 'Sales Invoice' as on_invoice, ifnull(si.is_return, 0) as is_return,
			sum(gle.debit) as debit, sum(gle.credit) as credit
		from `tabGL Entry` gle
		join `tabAccount` acc on acc.name = gle.account
		left join `tabSales Invoice` si on gle.voucher_type = 'Sales Invoice' and si.name = gle.voucher_no
		where gle.company = %(company)s and gle.posting_date between %(from_date)s and %(to_date)s
			and gle.is_cancelled = 0 and gle.voucher_type != 'Period Closing Voucher'
			and acc.root_type in ('Income', 'Expense')
		group by gle.account, acc.root_type, on_invoice, is_return
		""",
		filters,
		as_dict=True,
	)


def accounts(company):
	return {
		a.name: a
		for a in frappe.get_all(
			"Account",
			filters={"company": company},
			fields=["name", "account_name", "account_type", "root_type", "lft", "rgt", "is_group", "include_in_gross"],
			order_by="lft asc",
		)
	}


def direct_top_group(accs, company):
	"""The group right under the expense root that holds the company's cost of goods sold account
	(Direct Expenses in the standard chart): everything in it is a direct cost, whatever its name."""
	cogs = accs.get(frappe.get_cached_value("Company", company, "default_expense_account"))
	if not cogs:
		return None
	chain = [a for a in accs.values() if a.is_group and a.lft < cogs.lft and cogs.rgt < a.rgt]  # root first
	chain.append(cogs)
	return chain[1] if len(chain) > 1 else None


def direct_accounts(accs, company):
	"""Expense accounts counted before gross profit."""
	groups = [(a.lft, a.rgt) for a in accs.values() if a.is_group and a.include_in_gross]
	top = direct_top_group(accs, company)
	if top:
		groups.append((top.lft, top.rgt))
	return {
		a.name
		for a in accs.values()
		if a.root_type == "Expense"
		and (a.include_in_gross or a.account_type in DIRECT_TYPES or any(lft < a.lft and a.rgt < rgt for lft, rgt in groups))
	}


def cost_accounts(filters):
	"""The accounts a sales invoice posts its items' cost to."""
	names = set(
		frappe.db.sql_list(
			f"""select distinct sii.expense_account from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			where {invoice_condition()} and ifnull(sii.expense_account, '') != ''""",
			filters,
		)
	)
	default = frappe.get_cached_value("Company", filters.company, "default_expense_account")
	return names | ({default} if default else set())


def invoice_additions(filters):
	"""Discounts and taxes billed on the range's sales invoices (not returns), in company currency.
	A discount posted to its own account (discount accounting) is already a deduction in the ledger."""
	discounts = 0
	if not cint(frappe.db.get_single_value("Selling Settings", "enable_discount_accounting")):
		discounts = flt(
			frappe.db.sql(
				f"""select sum(si.base_discount_amount) from `tabSales Invoice` si
				where {invoice_condition()} and si.is_return = 0""",
				filters,
			)[0][0]
		) + flt(
			frappe.db.sql(
				f"""select sum(greatest(sii.base_price_list_rate - sii.base_rate, 0) * sii.qty)
				from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
				where {invoice_condition()} and si.is_return = 0""",
				filters,
			)[0][0]
		)
	# Taxes and charges billed to an account outside the profit and loss (VAT, a service charge held
	# for the staff); one billed to an income account is already in the sales.
	taxes = flt(
		frappe.db.sql(
			f"""select sum(t.base_tax_amount_after_discount_amount) from `tabSales Taxes and Charges` t
			join `tabSales Invoice` si on si.name = t.parent and t.parenttype = 'Sales Invoice'
			join `tabAccount` acc on acc.name = t.account_head
			where {invoice_condition()} and si.is_return = 0 and acc.root_type not in ('Income', 'Expense')""",
			filters,
		)[0][0]
	)
	return discounts, taxes


# ---------------------------------------------------------------------------------- fixed expenses


def fixed_accounts(company):
	"""Accounts marked fixed in Resto Settings; none marked: the salaries account, rent and
	depreciation."""
	settings = get_settings()
	marked = [r.account for r in settings.get("fixed_expense_accounts") or [] if r.account]
	if marked:
		return {a for a in marked if frappe.get_cached_value("Account", a, "company") == company}
	found = set(
		frappe.db.sql_list(
			"""select name from `tabAccount` where company = %(company)s and root_type = 'Expense' and is_group = 0
			and (lower(account_name) like 'rent%%' or lower(account_name) like '%% rent%%' or account_name like '%%إيجار%%'
				or account_type = 'Depreciation')""",
			{"company": company},
		)
	)
	if settings.get("salary_account") and frappe.get_cached_value("Account", settings.salary_account, "company") == company:
		found.add(settings.salary_account)
	return found


def spread(filters, fixed):
	"""{account: amount}: each fixed account's total for each month the range touches, times the
	share of that month's days in the range. Sales invoices never post these."""
	out = {}
	if not fixed:
		return out
	month = get_first_day(filters.from_date)
	while month <= filters.to_date:
		last = get_last_day(month)
		start, end = max(month, filters.from_date), min(last, filters.to_date)
		share = ((end - start).days + 1) / ((last - month).days + 1)
		for account, amount in frappe.db.sql(
			"""select account, sum(debit - credit) from `tabGL Entry`
			where company = %(company)s and account in %(accounts)s and posting_date between %(start)s and %(end)s
				and is_cancelled = 0 and voucher_type not in ('Period Closing Voucher', 'Sales Invoice')
			group by account""",
			{"company": filters.company, "accounts": list(fixed), "start": month, "end": last},
		):
			out[account] = out.get(account, 0) + flt(amount) * share
		month = add_days(last, 1)
	return out


# ---------------------------------------------------------------------------------- the statement


def statement(filters, spread_fixed=False):
	accs = accounts(filters.company)
	direct = direct_accounts(accs, filters.company)
	costs = cost_accounts(filters)
	company = frappe.get_cached_doc("Company", filters.company)

	sales = returns = deductions = cost_of_sales = 0
	other_income, direct_exp, indirect_exp = {}, {}, {}
	for r in ledger(filters):
		if r.root_type == "Income":
			amount = flt(r.credit) - flt(r.debit)
			if not r.on_invoice:
				other_income[r.account] = other_income.get(r.account, 0) + amount
			elif r.is_return:
				returns -= amount
			else:
				sales += amount
			continue
		amount = flt(r.debit) - flt(r.credit)
		if r.on_invoice:
			if r.account in costs or accs.get(r.account, frappe._dict()).account_type in DIRECT_TYPES:
				cost_of_sales += amount
			else:
				deductions += amount  # round off, write off, a discount account
		else:
			bucket = direct_exp if r.account in direct else indirect_exp
			bucket[r.account] = bucket.get(r.account, 0) + amount

	posted_profit = sales - returns - deductions - cost_of_sales + sum(other_income.values()) - sum(direct_exp.values()) - sum(indirect_exp.values())

	spread_accounts = set()
	if spread_fixed:
		fixed = {a for a in fixed_accounts(filters.company) if a in accs and accs[a].root_type == "Expense"}
		for account, amount in spread(filters, fixed).items():
			bucket = direct_exp if account in direct else indirect_exp
			bucket[account] = amount
			spread_accounts.add(account)
		for account in fixed - spread_accounts:  # posted only by sales invoices, or nothing this month
			direct_exp.pop(account, None)
			indirect_exp.pop(account, None)

	discounts, taxes = invoice_additions(filters)
	net_sales = sales - returns - deductions
	gross_sales = sales + discounts + taxes
	packaging = packaging_cost(filters) if cint(company.enable_perpetual_inventory) else 0
	food = cost_of_sales - packaging
	direct_total = sum(direct_exp.values())
	gross_profit = net_sales - food - packaging - direct_total
	other_total = sum(other_income.values())
	indirect_total = sum(indirect_exp.values())
	net_profit = gross_profit + other_total - indirect_total

	rows = []

	def line(key, label, amount, indent=0, bold=0, account=None):
		rows.append(
			{"key": key, "label": label, "amount": amount, "pct": flt(pct(amount, net_sales), 2), "indent": indent, "bold": bold,
			 "account": account}
		)

	def account_lines(key, amounts):
		for account, amount in sorted(amounts.items(), key=lambda a: -abs(a[1])):
			if not flt(amount, 2) and account not in spread_accounts:
				continue
			label = accs[account].account_name if account in accs else account
			if account in spread_accounts:
				label = _("{0} (spread)", context=CTX).format(label)
			line(key, label, amount, indent=2, account=account)

	line("gross_sales", _("Gross Sales", context=CTX), gross_sales, bold=1)
	line("discounts", _("Discounts and Rounding", context=CTX), discounts + deductions, indent=1)
	line("taxes", _("Taxes and Charges", context=CTX), taxes, indent=1)
	if flt(returns, 2):
		line("returns", _("Returns", context=CTX), returns, indent=1)
	line("net_sales", _("Net Sales", context=CTX), net_sales, bold=1)
	line("food", _("Food Cost", context=CTX), food, indent=1)
	line("packaging", _("Packaging", context=CTX), packaging, indent=1)
	if direct_exp:
		line("direct", _("Direct Expenses", context=CTX), direct_total, indent=1)
		account_lines("direct_account", direct_exp)
	line("gross_profit", _("Gross Profit", context=CTX), gross_profit, bold=1)
	if other_income:
		line("other_income", _("Other Income", context=CTX), other_total, indent=1)
		account_lines("other_income_account", other_income)
	line("indirect", _("Indirect Expenses", context=CTX), indirect_total, indent=1)
	account_lines("indirect_account", indirect_exp)
	line("net_profit", _("Net Profit", context=CTX), net_profit, bold=1)

	return frappe._dict(
		rows=rows,
		gross_sales=gross_sales,
		net_sales=net_sales,
		food=food,
		packaging=packaging,
		gross_profit=gross_profit,
		net_profit=net_profit,
		posted_profit=posted_profit,
		spread=bool(spread_accounts),
	)


def statement_columns():
	return [
		{"fieldname": "label", "label": _("Statement", context=CTX), "fieldtype": "Data", "width": 340},
		{"fieldname": "amount", "label": _("Amount", context=CTX), "fieldtype": "Currency", "width": 150},
		{"fieldname": "pct", "label": _("% of Net Sales", context=CTX), "fieldtype": "Percent", "width": 150},
		{"fieldname": "account", "label": _("Account"), "fieldtype": "Link", "options": "Account", "width": 200},
	]


def summary(pnl):
	def card(label, value, indicator):
		return {
			"label": f"{label} ({flt(pct(value, pnl.net_sales), 2)}%)",
			"value": value,
			"datatype": "Currency",
			"indicator": indicator,
		}

	return [
		card(_("Gross Sales", context=CTX), pnl.gross_sales, "Blue"),
		card(_("Net Sales", context=CTX), pnl.net_sales, "Blue"),
		card(_("Gross Profit", context=CTX), pnl.gross_profit, "Green" if pnl.gross_profit >= 0 else "Red"),
		card(_("Net Profit", context=CTX), pnl.net_profit, "Green" if pnl.net_profit >= 0 else "Red"),
	]


def statement_message(pnl):
	msg = _(
		"Every line is read from the general ledger for these dates; the net profit is the ledger's income less its expenses. "
		"Gross sales add back the discounts and the taxes billed; taxes are not income.",
		context=CTX,
	)
	if pnl.spread:
		msg += " " + _(
			"Fixed expenses are spread over the days of their month, so the net profit differs from the ledger's ({0}).",
			context=CTX,
		).format(frappe.format_value(pnl.posted_profit, {"fieldtype": "Currency"}))
	return msg


# ---------------------------------------------------------------------------------- cost of goods sold


def cogs_rows(filters):
	"""The items sold in the range (packaging included, the delivery fee left out), each with the
	cost the stock ledger posted for it: their total is the cost of sales of the statement."""
	actual = cost_by_invoice_row(filters)
	skip = excluded_items()
	items = {}
	for r in frappe.db.sql(
		f"""select sii.name, sii.item_code, sii.item_name, sii.item_group, sii.stock_qty as qty,
			sii.base_net_amount as sales, ifnull(sii.resto_packaging, '') != '' as packaging
		from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
		where {invoice_condition()}""",
		filters,
		as_dict=True,
	):
		if r.item_code in skip:
			continue
		i = items.setdefault(
			r.item_code,
			frappe._dict(item_code=r.item_code, item_name=r.item_name, item_group=r.item_group, qty=0, cost=0, sales=0,
			             packaging=cint(r.packaging)),
		)
		i.qty += flt(r.qty)
		i.cost += actual.get(r.name, 0)
		i.sales += flt(r.sales)
	rows = sorted(items.values(), key=lambda i: -i.cost)
	for i in rows:
		i.unit_cost = i.cost / i.qty if i.qty else 0
		i.margin = flt(pct(i.sales - i.cost, i.sales), 2) if i.sales else None
	return rows


def cogs_view(filters):
	rows = cogs_rows(filters)
	cost = sum(i.cost for i in rows)
	sales = sum(i.sales for i in rows)
	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 170},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 180},
		{"fieldname": "item_group", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 140},
		{"fieldname": "qty", "label": _("Qty Sold"), "fieldtype": "Float", "width": 90},
		{"fieldname": "unit_cost", "label": _("Unit Cost", context=CTX), "fieldtype": "Currency", "width": 110},
		{"fieldname": "cost", "label": _("Total Cost", context=CTX), "fieldtype": "Currency", "width": 130},
		{"fieldname": "sales", "label": _("Net Sales", context=CTX), "fieldtype": "Currency", "width": 130},
		{"fieldname": "margin", "label": _("Margin %", context=CTX), "fieldtype": "Percent", "width": 100},
	]
	data = [*rows]
	if rows:
		data.append(
			{"item_code": None, "item_name": _("Total"), "cost": cost, "sales": sales, "margin": flt(pct(sales - cost, sales), 2) if sales else None,
			 "bold": 1}
		)
	report_summary = [
		{"label": _("Total Cost", context=CTX), "value": cost, "datatype": "Currency", "indicator": "Orange"},
		{"label": _("Net Sales", context=CTX), "value": sales, "datatype": "Currency", "indicator": "Blue"},
		{"label": _("Margin %", context=CTX), "value": flt(pct(sales - cost, sales), 2), "datatype": "Percent",
		 "indicator": "Green" if sales >= cost else "Red"},
	]
	return columns, data, _(
		"The cost of each item is what the stock ledger posted when it was sold; a dish's cost is its ingredients'. "
		"Packaging billed with takeaway and delivery orders is listed with its cost and no sales. "
		"Sales here are the items' amounts; the statement's net sales also take in the bills' rounding and write-offs.",
		context=CTX,
	), None, report_summary
