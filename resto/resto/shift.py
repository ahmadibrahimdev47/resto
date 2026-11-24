"""The cashier's POSNext shift, opened and closed from the floor screen (D-199).

POSNext keeps the shift: its opening and closing documents are made by POSNext's own code, so its
reports, its closing and its rules stay as they are; resto only gives the cashier the screen. The
closing amounts the cashier counted are the only thing taken from the browser: what was expected is
worked out again on the server when the shift is closed."""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from resto.resto.billing import get_open_shift
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
	return bool(cint(frappe.db.get_value("POS Settings", {"pos_profile": profile}, "hide_expected_amount")))


def _own_shift():
	profile = _profile()
	name = get_open_shift(frappe.session.user, profile)
	if not name:
		frappe.throw(_("You have no open shift on POS Profile {0}").format(frappe.bold(profile)))
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
	profile = settings.pos_profile
	name = get_open_shift(frappe.session.user, profile) if profile else None
	out = {"required": cint(settings.require_pos_shift), "pos_profile": profile, "shift": name}
	if name:
		out["since"] = frappe.db.get_value("POS Opening Shift", name, "period_start_date")
	else:
		# POSNext allows one open shift per user: one on another POS Profile is closed in POSNext.
		other = frappe.db.get_value(
			"POS Opening Shift",
			{"user": frappe.session.user, "docstatus": 1, "status": "Open", "pos_profile": ("!=", profile or "")},
			"pos_profile",
		)
		out["other_profile"] = other
	return out


@frappe.whitelist()
def opening_data():
	require_role(CASHIER)
	profile = _profile()
	return {"pos_profile": profile, "modes": [m.mode_of_payment for m in _modes(profile)]}


@frappe.whitelist(methods=["POST"])
def open_shift(amounts=None):
	"""Open the cashier's shift on the restaurant's POS Profile, with the cash in the drawer."""
	require_role(CASHIER)
	if not posnext_installed():
		frappe.throw(_("POSNext is not installed"))
	profile = _profile()
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


@frappe.whitelist()
def closing_data():
	"""What the close dialog shows: each payment method, with what it should hold unless POSNext
	counts blind."""
	require_role(CASHIER)
	profile, name = _own_shift()
	data = _closing(name)
	blind = _blind(profile)
	return {
		"shift": name,
		"blind": blind,
		"invoices": len(data.get("pos_transactions") or []),
		"rows": [
			{
				"mode_of_payment": r.get("mode_of_payment"),
				"opening_amount": flt(r.get("opening_amount")),
				"expected_amount": None if blind else flt(r.get("expected_amount")),
			}
			for r in data.get("payment_reconciliation") or []
		],
	}


@frappe.whitelist(methods=["POST"])
def close_shift(amounts):
	"""Close the cashier's shift with the amounts counted, through POSNext's own closing."""
	require_role(CASHIER)
	_profile_name, name = _own_shift()
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
	rows = frappe.get_all(
		"POS Closing Shift Detail",
		filters={"parent": closing, "parenttype": "POS Closing Shift"},
		fields=["mode_of_payment", "expected_amount", "closing_amount", "difference"],
		order_by="idx",
	)
	return {"closing": closing, "rows": rows}
