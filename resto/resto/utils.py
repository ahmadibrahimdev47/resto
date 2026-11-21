"""Small shared helpers: roles, realtime and settings access."""

import frappe
from frappe import _

MANAGER = "Resto Manager"
WAITER = "Resto Waiter"
CASHIER = "Resto Cashier"
KITCHEN = "Resto Kitchen"
HOST = "Resto Host"
ALL_ROLES = (MANAGER, WAITER, CASHIER, KITCHEN, HOST)

# These roles can do everything a Resto role can.
SUPER_ROLES = ("System Manager", MANAGER)

REALTIME_EVENT = "resto_update"


def has_role(*roles):
	user_roles = set(frappe.get_roles())
	return bool(user_roles & set(SUPER_ROLES + roles))


def require_role(*roles):
	"""Throw unless the session user holds one of the roles (managers always pass)."""
	if not has_role(*roles):
		frappe.throw(_("You are not allowed to do this."), frappe.PermissionError)


def publish(kind, **data):
	"""Tell open floor and kitchen screens that something changed (after the commit)."""
	frappe.publish_realtime(REALTIME_EVENT, {"kind": kind, **data}, after_commit=True)


def get_settings():
	from resto.resto.doctype.resto_settings.resto_settings import get_settings as _get

	return _get()


def posnext_installed():
	return "pos_next" in frappe.get_installed_apps()
