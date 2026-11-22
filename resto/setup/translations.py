"""Resto's Arabic wins over apps installed after it.

Frappe merges app translation files in installed-app order, so an app installed after Resto
(POSNext) overrides Resto's wording of the same text ("Submit": «ترحيل»). Translation records
beat every app file, so for the few texts in OVERRIDE where a later app disagrees, Resto keeps a
Translation record with its own wording. Run on migrate and whenever another app is installed.

Only an allowlist: elsewhere a later app's wording fits its own screens (POSNext's "OK" button,
its "Free" gift item), and Resto's own screens use the "Resto" context instead.

Resto remembers what it wrote (a global default: key -> [source, context, text]) and only changes
or removes records that still hold its own wording: a record the user wrote or edited is left alone.
"""

import csv
import os

import frappe

LANG = "ar"
WRITTEN = "resto_translation_overrides"
# Texts whose Resto wording must hold on every screen (D-180: a document is posted, not sent).
OVERRIDE = ("Submit", "Submitted")


def resto_rows():
	"""(source, context, translation) of Resto's own file."""
	path = frappe.get_app_path("resto", "translations", f"{LANG}.csv")
	rows = []
	with open(path, encoding="utf-8", newline="") as f:
		for row in csv.reader(f):
			if len(row) >= 2 and row[0] and row[1]:
				rows.append((row[0], row[2] if len(row) > 2 else "", row[1]))
	return rows


def app_translations(app):
	"""Translations an app's files give (keys "source" or "source:context")."""
	from frappe.translate import get_translation_dict_from_file

	out = {}
	path = frappe.get_app_path(app, "translations", f"{LANG}.csv")
	if os.path.exists(path):
		out.update(get_translation_dict_from_file(path, LANG, app))
	try:
		from frappe.gettext.translate import get_translations_from_mo

		out.update(get_translations_from_mo(LANG, app) or {})
	except Exception:
		pass  # no compiled .mo for this app
	return out


def key(source, context):
	return f"{source}:{context}" if context else source


def find(source, context):
	filters = {"language": LANG, "source_text": source, "context": context or ("is", "not set")}
	return frappe.db.get_value("Translation", filters, ["name", "translated_text"], as_dict=True)


def sync():
	apps = frappe.get_installed_apps()
	if "resto" not in apps or not frappe.db.exists("Language", LANG):
		return
	theirs = {}
	for app in apps[apps.index("resto") + 1 :]:
		theirs.update(app_translations(app))

	wanted = {}
	for source, context, text in resto_rows():
		k = key(source, context)
		if k in OVERRIDE and k in theirs and theirs[k] != text:
			wanted[k] = (source, context, text)

	written = frappe.parse_json(frappe.db.get_global(WRITTEN) or "{}") or {}
	changed, removed = False, 0
	for k, (source, context, text) in wanted.items():
		row = find(source, context)
		ours = written.get(k)
		if not row:
			frappe.get_doc(
				{"doctype": "Translation", "language": LANG, "source_text": source, "context": context, "translated_text": text}
			).insert(ignore_permissions=True)
		elif ours and row.translated_text == ours[2]:
			if row.translated_text == text:
				continue  # already right
			frappe.db.set_value("Translation", row.name, "translated_text", text)
		else:
			# The user's own record (or one the user edited): left as it is, and no longer Resto's.
			changed = written.pop(k, None) is not None or changed
			continue
		written[k] = [source, context, text]
		changed = True

	# A text no longer in conflict: remove Resto's record, unless the user changed it since.
	for k in [k for k in written if k not in wanted]:
		source, context, text = written.pop(k)
		row = find(source, context)
		if row and row.translated_text == text:
			frappe.delete_doc("Translation", row.name, ignore_permissions=True, force=True)
			removed += 1
		changed = True

	frappe.db.set_global(WRITTEN, frappe.as_json(written))
	frappe.db.commit()
	# Shown in the migrate output, so a deploy can see what happened.
	print(f"Resto translation overrides: {len(written)} kept ({', '.join(sorted(written)) or 'none'}), {removed} removed")
	if changed:
		from frappe.translate import clear_cache

		clear_cache()
