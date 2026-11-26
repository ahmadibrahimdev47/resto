"""Watch the daily backup and its off-site copy (D-256).

The site's backups are written to sites/<site>/private/backups by `bench backup`. A copy kept on
the same machine is lost with it, so an upload script copies each night's backup off the machine
and then marks it here: it calls `mark_offsite_ok` (as a System Manager, with an API key) or
writes the time to private/backups/.offsite_ok. When the newest backup or the off-site mark is
older than Resto Settings' limit (26 hours by default), managers get a notification (and an email
when outgoing mail is set up) and a red bar on the desk and the floor screen until it is fixed.
Off by default: development and test sites have no nightly backup.
"""

import os
import time
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import cint, convert_utc_to_system_timezone, flt, format_datetime, get_datetime

from resto.resto.utils import MANAGER

WATCHERS = (MANAGER, "System Manager")
MARKER = ".offsite_ok"
DEFAULT_HOURS = 26
NOTIFIED_KEY = "resto_backup_alert_sent"


def backups_dir():
	return frappe.get_site_path("private", "backups")


def _settings():
	from resto.resto.utils import get_settings

	settings = get_settings()
	return cint(settings.get("watch_backups")), cint(settings.get("backup_max_age_hours")) or DEFAULT_HOURS


def _newest_backup():
	"""(path, unix time) of the newest database backup, or (None, None)."""
	folder = backups_dir()
	if not os.path.isdir(folder):
		return None, None
	best = (None, None)
	for name in os.listdir(folder):
		if "-database" not in name or not (name.endswith(".sql.gz") or name.endswith(".sql")):
			continue
		path = os.path.join(folder, name)
		at = os.path.getmtime(path)
		if best[1] is None or at > best[1]:
			best = (path, at)
	return best


def _offsite_time():
	"""Unix time of the last off-site upload: the time written in the mark, else the mark's own time."""
	path = os.path.join(backups_dir(), MARKER)
	if not os.path.isfile(path):
		return None
	try:
		with open(path) as f:
			text = f.read().strip()
		if text.replace(".", "", 1).isdigit():
			return flt(text)  # seconds since 1970, as `date +%s` writes them
		if text:
			return get_datetime(text).timestamp()  # an ISO time; with its offset, any time zone
	except Exception:
		pass
	return os.path.getmtime(path)


def _when(at):
	"""A unix time in the site's time zone, for people."""
	if not at:
		return None
	local = convert_utc_to_system_timezone(datetime.fromtimestamp(at, timezone.utc)).replace(tzinfo=None)
	return format_datetime(local, "dd-MM-yyyy HH:mm")


def status():
	"""What the watch sees now: the newest backup, the last off-site upload and what is wrong."""
	enabled, hours = _settings()
	path, backup_at = _newest_backup()
	offsite_at = _offsite_time()
	limit = time.time() - hours * 3600
	problems = []
	if not backup_at:
		problems.append(_("No backup found"))
	elif backup_at < limit:
		problems.append(_("Backup stopped since {0}").format(_when(backup_at)))
	if not offsite_at:
		problems.append(_("No off-site copy recorded yet"))
	elif offsite_at < limit:
		problems.append(_("Off-site copy stopped since {0}").format(_when(offsite_at)))
	return {
		"enabled": enabled,
		"max_age_hours": hours,
		"last_backup": _when(backup_at),
		"last_backup_file": os.path.basename(path) if path else None,
		"last_offsite": _when(offsite_at),
		"ok": not problems,
		"problems": problems,
	}


def _is_watcher():
	return bool(set(frappe.get_roles()) & set(WATCHERS))


def alert_for_user():
	"""The problems the session user should see now, or None (watch off, all fine, not a manager)."""
	if frappe.session.user == "Guest" or not _is_watcher() or not _settings()[0]:
		return None
	s = status()
	return None if s["ok"] else {"problems": s["problems"]}


@frappe.whitelist()
def alert():
	"""For the red bar on the desk and the floor screen (checked again every hour)."""
	return alert_for_user()


@frappe.whitelist()
def get_status():
	"""For the status block in Resto Settings."""
	if not _is_watcher():
		frappe.throw(_("You are not allowed to do this."), frappe.PermissionError)
	return status()


@frappe.whitelist(methods=["POST"])
def mark_offsite_ok():
	"""Called by the upload script once the night's backup is safely off this machine."""
	frappe.only_for("System Manager")
	folder = backups_dir()
	os.makedirs(folder, exist_ok=True)
	with open(os.path.join(folder, MARKER), "w") as f:
		f.write(str(int(time.time())))
	return _when(time.time())


def _watchers():
	"""(user, email) of every enabled desk user who is a manager."""
	rows = frappe.db.sql(
		"""
		select distinct u.name, u.email from `tabUser` u join `tabHas Role` r on r.parent = u.name and r.parenttype = 'User'
		where u.enabled = 1 and u.user_type = 'System User' and r.role in %(roles)s and u.name != 'Guest'
		""",
		{"roles": WATCHERS},
	)
	return [(name, email) for name, email in rows]


def check():
	"""Hourly: tell the managers once a day while the backup or its off-site copy is late."""
	if not _settings()[0]:
		return None
	s = status()
	if s["ok"]:
		frappe.cache.delete_value(NOTIFIED_KEY)
		return s
	if frappe.cache.get_value(NOTIFIED_KEY):
		return s
	subject = _("Backup needs attention: {0}").format(" · ".join(s["problems"]))
	body = "<br>".join(frappe.utils.escape_html(p) for p in s["problems"])
	watchers = _watchers()
	for user, _email in watchers:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": body,
				"document_type": "Resto Settings",
				"document_name": "Resto Settings",
			}
		).insert(ignore_permissions=True)
	emails = [email for _user, email in watchers if email]
	if emails and frappe.db.exists("Email Account", {"enable_outgoing": 1, "default_outgoing": 1}):
		frappe.sendmail(recipients=emails, subject=subject, message=body)
	frappe.cache.set_value(NOTIFIED_KEY, 1, expires_in_sec=20 * 3600)
	return s
