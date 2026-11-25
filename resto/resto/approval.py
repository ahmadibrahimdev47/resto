"""Manager approval on the floor (D-204).

A waiter or cashier needs a manager for a few actions: a void after the kitchen got the item, an
order cancel with sent items, a discount above their limit (Resto Settings), and a bill left on the
account of a customer without a credit limit. The manager types their PIN on the same screen; it is
checked inside the action's own call, so the approval is for that action and that amount only and is
spent with it. Every approval and every wrong PIN is logged (Resto Approval Log). Five tries without
the right PIN lock the manager's PIN for 15 minutes.

The tries are counted in Redis, before the PIN is checked: the count never depends on the failed
request's database work (which is rolled back), and requests sent at the same moment all count.
"""

import hashlib
import hmac
import os
import unicodedata

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

from resto.resto.utils import CASHIER, MANAGER, WAITER, has_role, require_role

MAX_TRIES = 5
LOCK_MINUTES = 15
ITERATIONS = 200_000

# What each action is called in the log and on the approval dialog.
ACTIONS = {
	"void": "Void an item sent to the kitchen",
	"cancel": "Cancel an order with items sent to the kitchen",
	"discount": "Discount above the limit",
	"on_account": "Bill on the account of a customer without a credit limit",
}


class ApprovalRequired(frappe.ValidationError):
	pass


def _digits(pin):
	"""The PIN as ASCII digits: «٤٣٢١» typed on an Arabic keypad is 4321. None when not all digits."""
	pin = str(pin or "").strip()
	if not pin or not all(c.isdecimal() for c in pin):
		return None
	return "".join(str(unicodedata.decimal(c)) for c in pin)


def _hash(pin, salt=None):
	salt = salt or os.urandom(16).hex()
	digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITERATIONS).hex()
	return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def _matches(pin, stored):
	try:
		_algo, iterations, salt, digest = stored.split("$")
		got = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iterations)).hex()
	except Exception:
		return False
	return hmac.compare_digest(got, digest)


def is_approver(user):
	roles = set(frappe.get_roles(user))
	return bool(roles & {MANAGER, "System Manager"}) and bool(frappe.db.get_value("User", user, "enabled"))


# --------------------------------------------------------------------------- tries (Redis)


def _tries_key(user):
	return frappe.cache.make_key(f"resto_pin_tries|{user}")


def _lock_key(user):
	return f"resto_pin_locked|{user}"


def locked_until(user):
	return frappe.cache.get_value(_lock_key(user))


def _take_try(user):
	"""Count one try before the PIN is checked; the fifth without the right PIN locks it."""
	key = _tries_key(user)
	tries = cint(frappe.cache.incr(key))
	frappe.cache.expire(key, LOCK_MINUTES * 60)
	if tries > MAX_TRIES:
		until = str(add_to_date(now_datetime(), minutes=LOCK_MINUTES))
		frappe.cache.set_value(_lock_key(user), until, expires_in_sec=LOCK_MINUTES * 60)
		frappe.cache.delete(key)
		return tries, until
	return tries, None


def _clear_tries(user):
	frappe.cache.delete(_tries_key(user))
	frappe.cache.delete_value(_lock_key(user))


# --------------------------------------------------------------------------- log


def _log(action, reference, amount, approver, result, details=None):
	log = frappe.get_doc(
		{
			"doctype": "Resto Approval Log",
			"action": (action or "")[:140],
			"reference": (reference or "")[:140],
			"amount": flt(amount),
			"approver": approver,
			"requested_by": frappe.session.user,
			"result": result,
			"details": details,
		}
	)
	log.flags.ignore_permissions = True
	log.insert()


def _log_failure(*args):
	"""A wrong or locked try is logged although the action fails: written after the request is rolled
	back (so nothing else of the failed action is kept), then committed. The lock itself is in Redis
	and never waits for this."""
	if frappe.flags.in_test:
		_log(*args)  # tests are not rolled back
		return

	def run():
		try:
			_log(*args)
			frappe.db.commit()  # nosemgrep: only the log row, after the rollback
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Resto approval log not written")

	frappe.db.after_rollback.add(run)


# --------------------------------------------------------------------------- approve


def approve(action, reference, amount=0, approver=None, pin=None, details=None):
	"""Pass when the session user is a manager; otherwise the approver's PIN must be right. Returns
	the approving user. Called inside the action itself, before it changes anything."""
	cache = frappe.flags.setdefault("resto_approvals", {})  # this request only
	key = (action, reference, flt(amount, 2))
	if key in cache:
		return cache[key]  # the same action checked twice in one call (floor, then billing)
	label = _(ACTIONS[action])
	if has_role():
		_log(label, reference, amount, frappe.session.user, "Approved", details)
		cache[key] = frappe.session.user
		return frappe.session.user
	if not approver or not pin:
		frappe.throw(_("This needs a manager's approval: {0}").format(label), ApprovalRequired)
	if approver == frappe.session.user or not is_approver(approver):
		frappe.throw(_("{0} cannot approve this").format(approver), ApprovalRequired)
	stored = frappe.db.get_value("Resto Manager PIN", {"user": approver}, "pin_hash")
	if not stored:
		frappe.throw(_("{0} has no PIN yet: they set it from the floor screen (My Approval PIN)").format(approver), ApprovalRequired)
	until = locked_until(approver)
	if until:
		_log_failure(label, reference, amount, approver, "Locked", details)
		frappe.throw(
			_("This PIN is locked after {0} wrong tries; try again after {1}").format(MAX_TRIES, frappe.format(until, "Datetime")),
			ApprovalRequired,
		)
	tries, locked = _take_try(approver)
	if locked:
		_log_failure(label, reference, amount, approver, "Locked", details)
		frappe.throw(_("Wrong PIN. It is locked for {0} minutes").format(LOCK_MINUTES), ApprovalRequired)
	typed = _digits(pin)
	if not typed or not _matches(typed, stored):
		_log_failure(label, reference, amount, approver, "Refused", details)
		if tries >= MAX_TRIES:
			frappe.cache.set_value(
				_lock_key(approver), str(add_to_date(now_datetime(), minutes=LOCK_MINUTES)), expires_in_sec=LOCK_MINUTES * 60
			)
			frappe.cache.delete(_tries_key(approver))
			frappe.throw(_("Wrong PIN. It is locked for {0} minutes").format(LOCK_MINUTES), ApprovalRequired)
		frappe.throw(_("Wrong PIN ({0} of {1} tries)").format(tries, MAX_TRIES), ApprovalRequired)
	_clear_tries(approver)
	_log(label, reference, amount, approver, "Approved", details)
	cache[key] = approver
	return approver


# --------------------------------------------------------------------------- endpoints


@frappe.whitelist()
def approvers():
	"""The managers who can approve on the floor (they have a PIN)."""
	require_role(WAITER, CASHIER)
	users = frappe.get_all("Resto Manager PIN", pluck="user")
	return [
		{"user": u, "full_name": frappe.db.get_value("User", u, "full_name") or u}
		for u in users
		if u != frappe.session.user and is_approver(u)
	]


@frappe.whitelist(methods=["POST"])
def set_my_pin(pin, password=None):
	"""A manager sets (or changes) their own approval PIN, 4 to 8 digits, after typing their login
	password: someone at a manager's open screen cannot plant a PIN they know."""
	require_role(MANAGER)
	user = frappe.session.user
	if not frappe.flags.in_test or password:
		from frappe.utils.password import check_password

		try:
			check_password(user, password or "")
		except frappe.AuthenticationError:
			frappe.throw(_("Your login password is not right"), frappe.PermissionError)
	pin = _digits(pin)
	if not pin or not 4 <= len(pin) <= 8:
		frappe.throw(_("The PIN is 4 to 8 digits"))
	name = frappe.db.get_value("Resto Manager PIN", {"user": user}, "name", for_update=True)
	doc = frappe.get_doc("Resto Manager PIN", name) if name else frappe.new_doc("Resto Manager PIN")
	doc.user = user
	doc.pin_hash = _hash(pin)
	doc.flags.resto_pin = True
	doc.flags.ignore_permissions = True
	doc.save()
	_clear_tries(user)  # a new PIN unlocks it
	return True
