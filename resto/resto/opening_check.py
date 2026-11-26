"""The drawer counted at the opening of a shift, against its books (D-258).

What a shift opens with must already be in the drawer's cash account: the last close left it
there (its over / short was booked), less what was dropped to the main safe since. When the count
is more, money came in that the books do not know: change from the main safe (posted now, with a
manager's PIN: Dr drawer, Cr main safe) or something to look into later (the shift is opened and
marked). When it is less, money left the drawer between two shifts: the shift is marked and the
managers are told; nothing is posted. Where the drawer's account is shared (another POS Profile,
or another cashier's open shift on the same one) its balance is not this drawer's, so it is not
compared. Counting blind, the cashier is not shown the books' figure.
"""

import frappe
from frappe import _
from frappe.utils import flt, fmt_money, nowdate

from resto.resto.utils import CASHIER, MANAGER, require_role

MATCHES = "Matches"
CHANGE = "Change from the Main Safe"
REVIEW = "To Review"
SHORT = "Short Since Last Close"
NOT_CHECKED = "Not Checked"
MORE = "More"  # before the cashier chose CHANGE or REVIEW
EPS = 0.005


def _account(mode, company):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

	return (get_bank_cash_account(mode, company) or {}).get("account")


def _shared(profile, company, account):
	"""The account's balance is not this drawer's alone."""
	from resto.resto import shift as shifts

	if frappe.db.exists("Resto Shift", {"status": "Open", "pos_profile": profile, "user": ("!=", frappe.session.user)}):
		return True
	for other in frappe.get_all("POS Profile", filters={"company": company, "disabled": 0, "name": ("!=", profile)}, pluck="name"):
		for m in shifts._modes(other):
			if frappe.get_cached_value("Mode of Payment", m.mode_of_payment, "type") == "Cash" and _account(m.mode_of_payment, company) == account:
				return True
	return False


def compare(profile, company, amounts):
	"""Per cash method of the profile: what was counted, what the books hold, and how they compare."""
	from resto.resto import cash_guard
	from resto.resto import shift as shifts

	rows = []
	for m in shifts._modes(profile):
		mode = m.mode_of_payment
		if frappe.get_cached_value("Mode of Payment", mode, "type") != "Cash":
			continue
		account = _account(mode, company)
		if not account:
			continue
		counted = flt(amounts.get(mode), 2)
		if _shared(profile, company, account):
			rows.append({"mode_of_payment": mode, "account": account, "counted": counted, "check": NOT_CHECKED})
			continue
		book = flt(cash_guard.balance(account, company, nowdate()), 2)
		difference = flt(counted - book, 2)
		check = MATCHES if abs(difference) < EPS else (MORE if difference > 0 else SHORT)
		rows.append({"mode_of_payment": mode, "account": account, "counted": counted, "book": book, "difference": difference, "check": check})
	return rows


def _money(value, currency):
	return fmt_money(abs(flt(value)), currency=currency)


@frappe.whitelist()
def preview(amounts):
	"""For the open dialog, before Open Shift: each cash method whose count is not what the books hold."""
	import json

	from resto.resto import shift as shifts

	require_role(CASHIER)
	profile = shifts._profile()
	company, currency = frappe.db.get_value("POS Profile", profile, ["company", "currency"])
	amounts = json.loads(amounts) if isinstance(amounts, str) else (amounts or {})
	blind = shifts._blind()
	out = []
	for r in compare(profile, company, amounts):
		if r["check"] == MORE:
			message = (
				_("The drawer holds more than the books. Where did it come from?")
				if blind
				else _("{0} more in the drawer than the books ({1}). Where did it come from?").format(
					_money(r["difference"], currency), _money(r["book"], currency)
				)
			)
		elif r["check"] == SHORT:
			message = (
				_("The drawer holds less than the last close left in it")
				if blind
				else _("{0} short of the last close").format(_money(r["difference"], currency))
			)
		else:
			continue
		out.append({"mode_of_payment": r["mode_of_payment"], "more": r["check"] == MORE, "message": message})
	return out


def notify(shift, rows):
	"""Tell the managers of an opening that did not match the books."""
	marked = [r for r in rows if r["check"] in (REVIEW, SHORT)]
	if not marked:
		return
	lines = [
		_("{0}: counted {1}, books {2} ({3})").format(
			_(r["mode_of_payment"]), _money(r["counted"], shift.currency), _money(r["book"], shift.currency), _(r["check"])
		)
		for r in marked
	]
	subject = _("Shift {0} opened with a drawer that does not match the books").format(shift.name)
	shift.add_comment("Info", "<br>".join([subject, *lines]))
	users = frappe.db.sql(
		"""select distinct u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name and r.parenttype = 'User'
		where u.enabled = 1 and u.user_type = 'System User' and r.role = %s""",
		MANAGER,
		pluck=True,
	)
	for user in users:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": "<br>".join(frappe.utils.escape_html(x) for x in lines),
				"document_type": "Resto Shift",
				"document_name": shift.name,
			}
		).insert(ignore_permissions=True)
