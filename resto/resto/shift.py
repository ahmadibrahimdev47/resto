"""The cashier's POSNext shift, opened and closed from the floor screen (D-199).

POSNext keeps the shift: its opening and closing documents are made by POSNext's own code, so its
reports, its closing and its rules stay as they are; resto only gives the cashier the screen. The
closing amounts the cashier counted are the only thing taken from the browser: what was expected is
worked out again on the server when the shift is closed."""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.utils import CASHIER, get_settings, posnext_installed, require_role


def _profile():
	profile = get_settings().pos_profile
	if not profile:
		frappe.throw(_("Set the POS Profile in Restaurant Settings"))
	return profile


def _modes(profile):
	"""The profile's payment methods a shift opens with (POSNext leaves out wallets)."""
	try:
		from pos_next.api.utilities import get_wallet_payment_modes

		wallets = set(get_wallet_payment_modes() or [])
	except ImportError:
		wallets = set()
	rows = frappe.get_all(
		"POS Payment Method",
		filters={"parent": profile, "parenttype": "POS Profile"},
		fields=["mode_of_payment", "default"],
		order_by="idx",
	)
	return [r for r in rows if r.mode_of_payment not in wallets]


def _blind(profile):
	"""POSNext's "hide expected amount": the cashier counts the drawer without seeing what is expected."""
	if not frappe.db.exists("DocType", "POS Settings") or not frappe.get_meta("POS Settings").has_field("hide_expected_amount"):
		return False
	filters = {"pos_profile": profile}
	if frappe.get_meta("POS Settings").has_field("enabled"):
		filters["enabled"] = 1
	return bool(cint(frappe.db.get_value("POS Settings", filters, "hide_expected_amount")))


def _user_shift():
	"""(POS Profile, shift) the user has open: POSNext allows one per user, on any profile."""
	row = frappe.db.get_value(
		"POS Opening Shift",
		{"user": frappe.session.user, "docstatus": 1, "status": "Open", "pos_closing_shift": ("is", "not set")},
		["pos_profile", "name"],
		order_by="period_start_date desc",
	)
	return tuple(row) if row else (None, None)


def _lock_user():
	"""One shift action of a user at a time (a double tap, two devices)."""
	frappe.db.sql("select name from `tabUser` where name = %s for update", frappe.session.user)


def _own_shift(lock=False):
	if lock:
		_lock_user()
	profile, name = _user_shift()
	if not name:
		frappe.throw(_("You have no open shift"))
	if lock:
		# A bill being settled on this shift finishes first, and none starts until it is closed.
		# A locking read sees a close another request just committed (a plain one, this snapshot).
		if frappe.db.get_value("POS Opening Shift", name, "status", for_update=True) != "Open":
			frappe.throw(_("This shift is already closed"))
	return profile, name


def _closing(name):
	"""POSNext's closing of the shift, worked out now (what each payment method should hold)."""
	from pos_next.pos_next.doctype.pos_closing_shift.pos_closing_shift import make_closing_shift_from_opening

	opening = frappe.get_doc("POS Opening Shift", name)
	return make_closing_shift_from_opening(json.dumps(opening.as_dict(), default=str))


def status():
	"""The cashier's shift, for the floor screen (cashiers only; None without POSNext)."""
	if not posnext_installed() or not frappe.db.exists("DocType", "POS Opening Shift"):
		return None
	settings = get_settings()
	profile, name = _user_shift()
	out = {"required": cint(settings.require_pos_shift), "pos_profile": settings.pos_profile, "shift": name}
	if name:
		out["since"] = frappe.db.get_value("POS Opening Shift", name, "period_start_date")
		# Open on another profile (from POSNext): the restaurant's bills need one on its own profile.
		out["other_profile"] = profile if profile != settings.pos_profile else None
	return out


@frappe.whitelist()
def opening_data():
	"""What POSNext's opening shows: the POS Profile (the restaurant's), its company, warehouse and
	currency, the cashier, now, and each payment method for its opening balance."""
	require_role(CASHIER)
	profile = _profile()
	p = frappe.db.get_value("POS Profile", profile, ["company", "warehouse", "currency"], as_dict=True) or {}
	return {
		"pos_profile": profile,
		"company": p.get("company"),
		"warehouse": p.get("warehouse"),
		"currency": p.get("currency"),
		"cashier": frappe.utils.get_fullname(frappe.session.user),
		"now": str(frappe.utils.now_datetime()),
		"open_shift": _user_shift()[1],
		"modes": [m.mode_of_payment for m in _modes(profile)],
	}


@frappe.whitelist(methods=["POST"])
def open_shift(amounts=None):
	"""Open the cashier's shift on the restaurant's POS Profile, with the cash in the drawer."""
	require_role(CASHIER)
	if not posnext_installed():
		frappe.throw(_("POSNext is not installed"))
	profile = _profile()
	_lock_user()
	# A locking read: an opening another request just committed is seen (a plain read would not).
	already = frappe.db.sql(
		"""select name from `tabPOS Opening Shift`
		where user = %s and docstatus = 1 and status = 'Open' limit 1 for update""",
		frappe.session.user,
	)
	if already:
		frappe.throw(_("You already have an open shift: {0}").format(already[0][0]))
	if not frappe.db.exists("POS Profile User", {"parent": profile, "parenttype": "POS Profile", "user": frappe.session.user}):
		frappe.throw(_("You are not a user of POS Profile {0}; ask the manager to add you").format(frappe.bold(profile)))
	amounts = json.loads(amounts) if isinstance(amounts, str) else (amounts or {})
	details = []
	for m in _modes(profile):
		amount = flt(amounts.get(m.mode_of_payment))
		if amount < 0:
			frappe.throw(_("{0}: the opening amount cannot be negative").format(m.mode_of_payment))
		details.append({"mode_of_payment": m.mode_of_payment, "opening_amount": amount})
	from pos_next.api.shifts import create_opening_shift

	data = create_opening_shift(profile, frappe.db.get_value("POS Profile", profile, "company"), json.dumps(details))
	return data["pos_opening_shift"]["name"]


def _report(data):
	"""What POSNext's closing screen shows, from its closing data: the shift's sales, returns,
	expenses, money collected and on account, taxes, invoices, and each payment method."""
	txns = data.get("pos_transactions") or []
	sales = flt(data.get("sales_total") if data.get("sales_total") is not None else data.get("grand_total"))
	returns = flt(data.get("returns_total"))
	expenses = flt(data.get("expenses_total"))
	collected = data.get("collected_amount")
	outstanding = data.get("outstanding_total")
	return {
		"shift": data.get("pos_opening_shift"),
		"pos_profile": data.get("pos_profile"),
		"user": data.get("user"),
		"period_start": data.get("period_start_date"),
		"period_end": data.get("period_end_date"),
		"summary": {
			"gross_sales": sales,
			"returns_total": returns,
			"returns_count": cint(data.get("returns_count")),
			"sales_count": cint(data.get("sales_count")),
			"expenses_total": expenses,
			"expenses_count": cint(data.get("expenses_count")),
			"net_sales": flt(data.get("grand_total")),
			"net_total": flt(data.get("net_total")),
			"total_quantity": flt(data.get("total_quantity")),
			"collected": flt(collected if collected is not None else sum(flt(t.get("collected_amount")) for t in txns)),
			"outstanding": flt(outstanding if outstanding is not None else sum(flt(t.get("outstanding_amount")) for t in txns)),
			"net_cash": sales - returns - expenses,
			"tax_total": sum(flt(t.get("amount")) for t in data.get("taxes") or []),
		},
		"taxes": [{"account_head": t.get("account_head"), "rate": flt(t.get("rate")), "amount": flt(t.get("amount"))} for t in data.get("taxes") or []],
		"invoices": [
			{
				"name": t.get("sales_invoice") or t.get("pos_invoice"),
				"posting_date": t.get("posting_date"),
				"customer": t.get("customer"),
				"grand_total": flt(t.get("grand_total")),
				"is_return": cint(t.get("is_return")),
				"outstanding_amount": flt(t.get("outstanding_amount")),
			}
			for t in txns
		],
		"expenses": [
			{"expense_account": e.get("expense_account"), "amount": flt(e.get("amount")), "remarks": e.get("remarks")}
			for e in data.get("pos_expenses") or []
		],
		"rows": [
			{
				"mode_of_payment": r.get("mode_of_payment"),
				"opening_amount": flt(r.get("opening_amount")),
				"expected_amount": flt(r.get("expected_amount")),
				"closing_amount": flt(r.get("closing_amount")) if r.get("closing_amount") is not None else None,
				"difference": flt(r.get("difference")) if r.get("difference") is not None else None,
			}
			for r in data.get("payment_reconciliation") or []
		],
	}


@frappe.whitelist()
def closing_data():
	"""What the close dialog shows, as POSNext's closing screen does. When POSNext counts blind
	(«إخفاء المبلغ المتوقع») only the payment methods are given until the shift is closed."""
	require_role(CASHIER)
	profile, name = _own_shift()
	report = _report(_closing(name))
	report["blind"] = _blind(profile)
	if report["blind"]:
		report = {
			"shift": report["shift"],
			"pos_profile": report["pos_profile"],
			"blind": True,
			"period_start": report["period_start"],
			"rows": [{"mode_of_payment": r["mode_of_payment"]} for r in report["rows"]],
		}
	# Orders still open on this profile: their bills are not in this shift.
	from resto.resto.doctype.resto_order.resto_order import ACTIVE

	report["open_orders"] = frappe.db.count("Resto Order", {"status": ("in", ACTIVE), "pos_profile": profile})
	return report


@frappe.whitelist(methods=["POST"])
def close_shift(amounts):
	"""Close the cashier's shift with the amounts counted, through POSNext's own closing."""
	require_role(CASHIER)
	_profile_name, name = _own_shift(lock=True)
	amounts = json.loads(amounts) if isinstance(amounts, str) else (amounts or {})
	data = _closing(name)
	for row in data.get("payment_reconciliation") or []:
		mode = row.get("mode_of_payment")
		if mode not in amounts or amounts[mode] in (None, ""):
			frappe.throw(_("Enter the amount counted for {0}").format(frappe.bold(mode)))
		if flt(amounts[mode]) < 0:
			frappe.throw(_("{0}: the amount counted cannot be negative").format(mode))
		row["closing_amount"] = flt(amounts[mode])
	from pos_next.pos_next.doctype.pos_closing_shift.pos_closing_shift import submit_closing_shift

	closing = submit_closing_shift(json.dumps(data, default=str))
	# The full report, as POSNext shows it after a close, with what the closing document saved.
	report = _report(data)
	saved = {
		r.mode_of_payment: r
		for r in frappe.get_all(
			"POS Closing Shift Detail",
			filters={"parent": closing, "parenttype": "POS Closing Shift"},
			fields=["mode_of_payment", "opening_amount", "expected_amount", "closing_amount", "difference"],
		)
	}
	for row in report["rows"]:
		s = saved.get(row["mode_of_payment"])
		if s:
			row.update({k: flt(s[k]) for k in ("opening_amount", "expected_amount", "closing_amount", "difference")})
	report["closing"] = closing
	return report
