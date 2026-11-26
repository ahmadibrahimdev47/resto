"""A cash account never goes below zero unnoticed (D-257).

Cash in a box cannot be less than nothing: a Cash-type account with a negative balance means a
recording mistake (cash that came in was not recorded, or an expense was posted twice). ERPNext
does not stop it. After a Journal Entry, Payment Entry or paid Purchase Invoice posts, the balance
of every Cash-type account it takes money from is worked out as of the posting date, this posting
included. Resto Settings «عند نزول الخزينة تحت الصفر»: Warn Only (the desk asks before submitting,
and a posting from elsewhere shows a warning) or Block the Posting. Resto's own postings (salary
entries, drawer expenses, cash purchases, staff advances, cash drops) are Journal Entries or
Purchase Invoices, so they are covered too; the drawer is also bounded by its shift.
"""

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

from resto.resto.utils import get_settings

WARN, BLOCK = "Warn Only", "Block the Posting"
EPS = 0.005


def mode():
	return get_settings().get("negative_cash") or WARN


def _is_cash(account):
	return frappe.get_cached_value("Account", account, "account_type") == "Cash"


def balance(account, company, posting_date):
	"""The account's balance (company currency) as of the date, from the ledger."""
	return flt(
		frappe.db.sql(
			"""select sum(debit) - sum(credit) from `tabGL Entry`
			where account = %s and company = %s and posting_date <= %s and is_cancelled = 0""",
			(account, company, posting_date),
		)[0][0]
	)


def _message(rows, company):
	currency = frappe.get_cached_value("Company", company, "default_currency")
	return "<br>".join(
		_("The balance of {0} would be {1} below zero after this posting. Check that all the cash that came in is recorded.").format(
			frappe.bold(r["account"]), frappe.bold(fmt_money(abs(r["balance"]), currency=currency))
		)
		for r in rows
	)


def _planned(doc):
	"""{account: money it takes out (credit - debit)} as the posting will book it, before it is booked."""
	out = {}

	def add(account, amount):
		if account:
			out[account] = out.get(account, 0) + flt(amount)

	if doc.doctype == "Journal Entry":
		for row in doc.accounts:
			add(row.account, flt(row.credit) - flt(row.debit))
	elif doc.doctype == "Payment Entry":
		if doc.payment_type in ("Pay", "Internal Transfer"):
			add(doc.paid_from, doc.base_paid_amount)
		if doc.payment_type in ("Receive", "Internal Transfer"):
			add(doc.paid_to, -flt(doc.base_received_amount))
	elif doc.doctype == "Purchase Invoice":
		if doc.is_paid and doc.cash_bank_account:
			add(doc.cash_bank_account, doc.base_paid_amount)
	elif doc.doctype == "Resto Salary Entry":
		add(doc.pay_from_account, doc.total_net)
	return out


def _below_zero(company, posting_date, taken):
	"""Cash accounts that `taken` ({account: money out}) leaves below zero, with their balance after."""
	rows = []
	for account, amount in taken.items():
		if amount <= EPS or not _is_cash(account):
			continue
		after = balance(account, company, posting_date)
		rows.append({"account": account, "balance": after})
	return [r for r in rows if r["balance"] < -EPS]


@frappe.whitelist()
def preview(doctype, name):
	"""For the desk before Submit: which cash accounts this saved draft would leave below zero."""
	if doctype not in ("Journal Entry", "Payment Entry", "Purchase Invoice", "Resto Salary Entry"):
		return None
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	if doc.docstatus != 0:
		return None
	rows = []
	for account, amount in _planned(doc).items():
		if amount <= EPS or not _is_cash(account):
			continue
		after = balance(account, doc.company, doc.posting_date) - amount
		if after < -EPS:
			rows.append({"account": account, "balance": after})
	if not rows:
		return None
	return {"mode": mode(), "message": _message(rows, doc.company)}


def check_posting(doc, method=None):
	"""on_submit of a Journal Entry, Payment Entry or Purchase Invoice: its GL entries are booked."""
	taken = {}
	for row in frappe.get_all(
		"GL Entry",
		filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		taken[row.account] = taken.get(row.account, 0) + flt(row.credit) - flt(row.debit)
	rows = _below_zero(doc.company, doc.posting_date, taken)
	if not rows:
		return
	if mode() == BLOCK:
		frappe.throw(_message(rows, doc.company), title=_("Cash Below Zero"))
	frappe.msgprint(_message(rows, doc.company), title=_("Cash Below Zero"), indicator="orange", alert=True)
