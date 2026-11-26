"""Watch the nightly backup and its off-site copy (D-256), on a folder of the test's own.

bench --site test.resto run-tests --module resto.tests.test_backup_watch
"""

import os
import shutil
import tempfile
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from resto.resto import backup_watch
from resto.tests import fixtures as fx
from resto.tests.test_floor_customers_shift import MANAGER_USER, _manager

HOUR = 3600


class TestBackupWatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()

	def setUp(self):
		frappe.set_user("Administrator")
		self.folder = tempfile.mkdtemp()
		self.patch = patch.object(backup_watch, "backups_dir", return_value=self.folder)
		self.patch.start()
		frappe.db.set_single_value("Resto Settings", {"watch_backups": 1, "backup_max_age_hours": 26})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.cache.delete_value(backup_watch.NOTIFIED_KEY)

	def tearDown(self):
		frappe.set_user("Administrator")
		self.patch.stop()
		shutil.rmtree(self.folder, ignore_errors=True)
		frappe.db.set_single_value("Resto Settings", {"watch_backups": 0, "backup_max_age_hours": 26})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.cache.delete_value(backup_watch.NOTIFIED_KEY)

	def backup(self, hours_ago, name="20261126_020000-site-database.sql.gz"):
		path = os.path.join(self.folder, name)
		with open(path, "w") as f:
			f.write("x")
		at = time.time() - hours_ago * HOUR
		os.utime(path, (at, at))

	def offsite(self, hours_ago):
		with open(os.path.join(self.folder, backup_watch.MARKER), "w") as f:
			f.write(str(int(time.time() - hours_ago * HOUR)))

	def notes(self):
		return frappe.get_all(
			"Notification Log", filters={"for_user": MANAGER_USER, "document_type": "Resto Settings"}, pluck="name"
		)

	def test_fresh_backup_and_copy_are_ok(self):
		self.backup(5)
		self.backup(30, "20261125_020000-site-database.sql.gz")  # older ones do not count
		self.offsite(4)
		s = backup_watch.status()
		self.assertTrue(s["ok"], s["problems"])
		self.assertEqual(s["last_backup_file"], "20261126_020000-site-database.sql.gz")
		self.assertTrue(s["last_offsite"])

	def test_a_late_backup_or_copy_tells_the_managers_once(self):
		before = set(self.notes())
		self.backup(30)
		s = backup_watch.status()
		self.assertFalse(s["ok"])
		self.assertEqual(len(s["problems"]), 2)  # late backup, and no copy recorded at all
		backup_watch.check()
		backup_watch.check()  # the next hour: no second notification the same day
		new = set(self.notes()) - before
		self.assertEqual(len(new), 1)
		frappe.set_user(MANAGER_USER)
		self.assertEqual(len(backup_watch.alert()["problems"]), 2)
		frappe.set_user("cashier@resto.test")
		self.assertIsNone(backup_watch.alert())  # the bar is for managers
		self.assertRaises(frappe.PermissionError, backup_watch.get_status)

		# Fixed: the bar goes, and a later failure is told again.
		frappe.set_user("Administrator")
		self.backup(1)
		backup_watch.mark_offsite_ok()
		self.assertTrue(backup_watch.check()["ok"])
		frappe.set_user(MANAGER_USER)
		self.assertIsNone(backup_watch.alert())
		frappe.set_user("Administrator")
		self.offsite(27)
		self.assertIn("Off-site", " ".join(backup_watch.status()["problems"]))
		backup_watch.check()
		self.assertEqual(len(set(self.notes()) - before), 2)

	def test_only_a_system_manager_marks_the_copy(self):
		frappe.set_user(MANAGER_USER)
		self.assertRaises(frappe.PermissionError, backup_watch.mark_offsite_ok)
		frappe.set_user("Administrator")
		backup_watch.mark_offsite_ok()
		self.assertTrue(os.path.isfile(os.path.join(self.folder, backup_watch.MARKER)))

	def test_watch_off_stays_quiet(self):
		frappe.db.set_single_value("Resto Settings", "watch_backups", 0)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		before = set(self.notes())
		self.assertIsNone(backup_watch.check())
		frappe.set_user(MANAGER_USER)
		self.assertIsNone(backup_watch.alert())
		frappe.set_user("Administrator")
		self.assertEqual(set(self.notes()) - before, set())
