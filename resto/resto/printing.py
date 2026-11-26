"""Server printing with copies: one print job per copy, each copy may carry its own label.

The label reaches the print format as ``doc.resto_copy_label`` (empty for an unlabelled copy).
Resto's own formats show it; a custom receipt format can show it the same way.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from resto.resto.utils import get_settings


RECEIPT_FORMAT = "Resto Receipt"
# Receipt printers: no page margins, and the print page banner never reaches the paper.
BROWSER_PAGE_STYLE = "<style>@page { margin: 0; } .action-banner, .print-hide { display: none !important; }</style>"


def copy_labels(rows):
	"""One label per copy row; no rows means one copy without a label."""
	return [cstr(r.label).strip() for r in rows or []] or [""]


def copy_specs(rows):
	"""One {label, show_sides} per bill or receipt copy row; no rows: one copy, no label, no sides."""
	return [{"label": cstr(r.label).strip(), "show_sides": cint(r.get("show_sides"))} for r in rows or []] or [
		{"label": "", "show_sides": 0}
	]


def _set_copy(doc, copy):
	"""Print-only attributes of one copy: resto_copy_label, and resto_show_sides for bills and receipts."""
	if isinstance(copy, dict):
		doc.resto_copy_label = copy.get("label") or ""
		doc.resto_show_sides = cint(copy.get("show_sides"))
	else:
		doc.resto_copy_label = copy


def print_copies(doctype, name, printer, print_format, labels, no_letterhead=1, extra=None):
	"""One print job per label. `extra` sets more print-only attributes on the document (resto_reprint)."""
	from frappe.utils.print_format import print_by_server

	for label in labels:
		doc = frappe.get_doc(doctype, name)
		_set_copy(doc, label)
		for key, value in (extra or {}).items():
			setattr(doc, key, value)
		print_by_server(doctype, name, printer, print_format, doc=doc, no_letterhead=no_letterhead)


def _run(title, doctype, name, printer, print_format, labels, no_letterhead=1, extra=None):
	"""Background job body. Never raises: a printer problem must not block the restaurant."""
	try:
		print_copies(doctype, name, printer, print_format, labels, no_letterhead=no_letterhead, extra=extra)
		return True
	except Exception:
		frappe.log_error(title=title)
		return False


def print_pages(doctype, name, print_format, labels, no_letterhead=1, extra=None):
	"""The print page once per copy, each with its label, for the browser to print (one job each).
	The callers checked the Resto roles: a User Permission or a print role must not stop the paper."""
	pages = []
	ignore = frappe.flags.ignore_print_permissions
	frappe.flags.ignore_print_permissions = True
	try:
		for label in labels:
			doc = frappe.get_doc(doctype, name)
			_set_copy(doc, label)
			for key, value in (extra or {}).items():
				setattr(doc, key, value)
			html = frappe.get_print(doctype, name, print_format, doc=doc, no_letterhead=no_letterhead)
			# The print page banner never reaches the paper.
			pages.append(html.replace("</head>", BROWSER_PAGE_STYLE + "</head>", 1))
	finally:
		frappe.flags.ignore_print_permissions = ignore
	return pages


# --------------------------------------------------------------------------- cashier printing method

SERVER_PRINTER = "Server Printer"
BROWSER_AUTO = "Browser Automatic"
BROWSER_BUTTON = "Browser Button"
NO_PRINTING = "No Printing"


def cashier_method(settings=None):
	settings = settings or get_settings()
	return settings.cashier_print_method or (SERVER_PRINTER if settings.cashier_printer else BROWSER_BUTTON)


def bill_mode(settings=None):
	"""server: the cashier printer prints it; browser: the screen that asked for the bill prints it;
	none: it is not printed."""
	settings = settings or get_settings()
	if bill_on_printer(settings):
		return "server"
	return "none" if cashier_method(settings) == NO_PRINTING else "browser"


def receipt_mode(settings=None):
	"""server: the cashier printer prints it after payment; auto: the cashier's browser prints it after
	payment; button: it prints with the Print Receipt button; none: not by itself (the button still
	prints it)."""
	settings = settings or get_settings()
	if receipt_on_printer(settings):
		return "server"
	return {BROWSER_AUTO: "auto", NO_PRINTING: "none"}.get(cashier_method(settings), "button")


# --------------------------------------------------------------------------- bill (pre-bill)


def bill_on_printer(settings=None):
	settings = settings or get_settings()
	return bool(
		cashier_method(settings) == SERVER_PRINTER and settings.cashier_printer and cint(settings.print_bill_on_printer)
	)


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
		copy_specs(settings.bill_copies),
	)


def bill_pages(order):
	"""The bill's pages for the browser, all its copies."""
	settings = get_settings()
	return print_pages("Resto Order", order, "Resto Pre-Bill", copy_specs(settings.bill_copies))


# --------------------------------------------------------------------------- receipt


def receipt_on_printer(settings=None):
	settings = settings or get_settings()
	return bool(
		cashier_method(settings) == SERVER_PRINTER and settings.cashier_printer and cint(settings.print_receipt_on_printer)
	)


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


def _receipt_format(pos_profile):
	"""Resto Settings "Receipt Print Format", else the print format of the POS Profile."""
	return (
		get_settings().receipt_print_format
		or (pos_profile and frappe.db.get_value("POS Profile", pos_profile, "print_format"))
		or None
	)


def _no_letterhead(print_format):
	"""Resto Receipt carries the restaurant name itself; another format keeps its letter head."""
	return 1 if print_format == RECEIPT_FORMAT else 0


def print_receipt(sales_invoice, pos_profile=None):
	settings = get_settings()
	if not settings.cashier_printer:
		return
	print_format = _receipt_format(pos_profile)
	printed_before = cint(frappe.db.get_value("Sales Invoice", sales_invoice, "resto_receipt_printed"))
	if _run(
		_("Receipt {0} was not printed").format(sales_invoice),
		"Sales Invoice",
		sales_invoice,
		settings.cashier_printer,
		print_format,
		copy_specs(settings.receipt_copies),
		no_letterhead=_no_letterhead(print_format),
		extra={"resto_reprint": printed_before},
	):
		frappe.db.set_value("Sales Invoice", sales_invoice, "resto_receipt_printed", 1, update_modified=False)


def _restaurant_invoice(sales_invoice):
	from resto.resto.utils import CASHIER, require_role

	require_role(CASHIER)
	row = frappe.db.get_value(
		"Sales Invoice", sales_invoice, ["resto_order", "pos_profile", "docstatus", "resto_receipt_printed"], as_dict=True
	)
	if not row or not row.resto_order:
		frappe.throw(_("Invoice {0} is not a restaurant invoice").format(sales_invoice))
	if row.docstatus != 1:
		frappe.throw(_("Invoice {0} is not submitted").format(sales_invoice))
	return row


@frappe.whitelist()
def receipt_pages(sales_invoice):
	"""The receipt's pages for the cashier's browser, all its copies, each with its label. A receipt
	printed before is marked as a reprint."""
	row = _restaurant_invoice(sales_invoice)
	settings = get_settings()
	print_format = _receipt_format(row.pos_profile)
	return print_pages(
		"Sales Invoice",
		sales_invoice,
		print_format,
		copy_specs(settings.receipt_copies),
		no_letterhead=_no_letterhead(print_format),
		extra={"resto_reprint": cint(row.resto_receipt_printed)},
	)


@frappe.whitelist()
def recent_receipts():
	"""The restaurant invoices of this business day (Resto Settings "Business Day Starts At"), newest
	first, to print a receipt again after its dialog closed: the cashier's own, or everyone's for a
	manager."""
	from frappe.utils import add_to_date, get_datetime, getdate, now_datetime

	from resto.resto.demand import day_start_hour
	from resto.resto.utils import CASHIER, has_role, require_role

	require_role(CASHIER)
	now = now_datetime()
	start = get_datetime(getdate(now)).replace(hour=day_start_hour())
	if now < start:
		start = add_to_date(start, days=-1)
	filters = {"docstatus": 1, "resto_order": ("is", "set"), "creation": (">=", start)}
	if not has_role():
		filters["owner"] = frappe.session.user
	return frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name", "resto_order", "resto_table", "resto_order_type", "grand_total", "rounded_total",
			"posting_time", "resto_receipt_printed", "is_return",
		],
		order_by="creation desc",
		limit=100,
	)


@frappe.whitelist(methods=["POST"])
def receipt_printed(sales_invoice):
	"""The cashier's browser printed the receipt: a retried payment does not print it again."""
	_restaurant_invoice(sales_invoice)
	frappe.db.set_value("Sales Invoice", sales_invoice, "resto_receipt_printed", 1, update_modified=False)
	return True


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
