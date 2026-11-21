"""Server printing with copies: one print job per copy, each copy may carry its own label.

The label reaches the print format as ``doc.resto_copy_label`` (empty for an unlabelled copy).
Resto's own formats show it; a custom receipt format can show it the same way.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from resto.resto.utils import get_settings


def copy_labels(rows):
	"""One label per copy row; no rows means one copy without a label."""
	return [cstr(r.label).strip() for r in rows or []] or [""]


def print_copies(doctype, name, printer, print_format, labels, no_letterhead=1):
	from frappe.utils.print_format import print_by_server

	for label in labels:
		doc = frappe.get_doc(doctype, name)
		doc.resto_copy_label = label
		print_by_server(doctype, name, printer, print_format, doc=doc, no_letterhead=no_letterhead)


def _run(title, doctype, name, printer, print_format, labels, no_letterhead=1):
	"""Background job body. Never raises: a printer problem must not block the restaurant."""
	try:
		print_copies(doctype, name, printer, print_format, labels, no_letterhead=no_letterhead)
		return True
	except Exception:
		frappe.log_error(title=title)
		return False


# --------------------------------------------------------------------------- bill (pre-bill)


def bill_on_printer(settings=None):
	settings = settings or get_settings()
	return bool(settings.cashier_printer and cint(settings.print_bill_on_printer))


def queue_bill(order):
	"""Queue the printed bill on the cashier printer. Returns True when queued."""
	settings = get_settings()
	if not bill_on_printer(settings):
		return False
	frappe.enqueue("resto.resto.printing.print_bill", order=order, queue="short", enqueue_after_commit=True)
	return True


def print_bill(order):
	settings = get_settings()
	if not settings.cashier_printer:
		return
	_run(
		_("Bill {0} was not printed").format(order),
		"Resto Order",
		order,
		settings.cashier_printer,
		"Resto Pre-Bill",
		copy_labels(settings.bill_copies),
	)


# --------------------------------------------------------------------------- receipt


def receipt_on_printer(settings=None):
	settings = settings or get_settings()
	return bool(settings.cashier_printer and cint(settings.print_receipt_on_printer))


def queue_receipt(sales_invoice, pos_profile=None):
	"""Queue the receipt of a newly paid invoice. Returns True when queued."""
	settings = get_settings()
	if not receipt_on_printer(settings):
		return False
	frappe.enqueue(
		"resto.resto.printing.print_receipt",
		sales_invoice=sales_invoice,
		pos_profile=pos_profile,
		queue="short",
		enqueue_after_commit=True,
	)
	return True


def print_receipt(sales_invoice, pos_profile=None):
	settings = get_settings()
	if not settings.cashier_printer:
		return
	print_format = (pos_profile and frappe.db.get_value("POS Profile", pos_profile, "print_format")) or None
	_run(
		_("Receipt {0} was not printed").format(sales_invoice),
		"Sales Invoice",
		sales_invoice,
		settings.cashier_printer,
		print_format,
		copy_labels(settings.receipt_copies),
		no_letterhead=0,
	)


# --------------------------------------------------------------------------- reprint


@frappe.whitelist(methods=["POST"])
def reprint(doctype, name):
	"""Print a bill or receipt again on the cashier printer, with all its copies.

	Kitchen tickets reprint from the kitchen screen (kitchen.kds_print)."""
	from resto.resto.utils import CASHIER, WAITER, require_role

	if doctype == "Resto Order":
		require_role(WAITER, CASHIER)
		if not queue_bill(name):
			frappe.throw(_("No cashier printer is set for bills in Resto Settings"))
	elif doctype == "Sales Invoice":
		require_role(CASHIER)
		if not frappe.db.get_value("Sales Invoice", name, "resto_order"):
			frappe.throw(_("Invoice {0} is not a restaurant invoice").format(name))
		pos_profile = frappe.db.get_value("Sales Invoice", name, "pos_profile")
		if not queue_receipt(name, pos_profile):
			frappe.throw(_("No cashier printer is set for receipts in Resto Settings"))
	else:
		frappe.throw(_("Cannot print {0}").format(doctype))
	return True
