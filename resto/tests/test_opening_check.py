"""The drawer counted at the opening of a shift, against its books (D-258).

bench --site test.resto run-tests --module resto.tests.test_opening_check
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from resto.resto import approval, cash_guard, opening_check, shift
from resto.tests import fixtures as fx
from resto.tests.test_drawer_cash import PIN, safe_account
from resto.tests.test_floor_customers_shift import CASHIER, MANAGER_USER, _manager
from resto.tests.test_staff_advance import cash_account

FIELDS = ("require_pos_shift", "blind_shift_count", "main_safe_account")


class TestOpeningCheck(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.ensure_setup()
		_manager()
		cls.safe = safe_account()
		cls.drawer = cash_account()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pop("resto_approvals", None)
		self._kept = {f: frappe.db.get_single_value("Resto Settings", f) for f in FIELDS}
		frappe.db.set_single_value("Resto Settings", {"require_pos_shift": 0, "blind_shift_count": 0, "main_safe_account": self.safe})
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		if frappe.db.exists("POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile"}) and not frappe.db.exists(
			"POS Profile User", {"parent": fx.PROFILE, "parenttype": "POS Profile", "user": CASHIER}
		):
			frappe.get_doc(
				{"doctype": "POS Profile User", "parent": fx.PROFILE, "parenttype": "POS Profile", "parentfield": "applicable_for_users", "user": CASHIER}
			).db_insert()
		frappe.set_user(MANAGER_USER)
		approval.set_my_pin(PIN)
		frappe.set_user("Administrator")
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		# The test site may have other POS Profiles on the same cash account: compared here.
		self.shared = patch.object(opening_check, "_shared", return_value=False)
		self.shared.start()

	def tearDown(self):
		self.shared.stop()
		frappe.set_user("Administrator")
		frappe.db.sql("update `tabResto Shift` set status = 'Closed' where status = 'Open'")
		frappe.db.set_single_value("Resto Settings", self._kept)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.flags.pop("resto_approvals", None)

	def book(self):
		return flt(cash_guard.balance(self.drawer, fx.COMPANY, nowdate()), 2)

	def open(self, amount, extra=None, approver=None, pin=None):
		frappe.set_user(CASHIER)
		name = shift.open_shift(json.dumps({"Cash": amount}), extra, approver, pin)
		return frappe.get_doc("Resto Shift", name)

	def close(self):
		frappe.set_user(CASHIER)
		rows = shift.closing_data()["rows"]
		# Every method of the profile is counted (the test profile has a card method too).
		shift.close_shift(json.dumps({r["mode_of_payment"]: r["expected_amount"] for r in rows}))
		return next(r for r in rows if r["mode_of_payment"] == "Cash")

	def cash(self, doc):
		return next(p for p in doc.payments if p.mode_of_payment == "Cash")

	def notes(self, shift_name):
		return frappe.get_all(
			"Notification Log", filters={"for_user": MANAGER_USER, "document_type": "Resto Shift", "document_name": shift_name}, pluck="name"
		)

	def test_a_count_that_matches_the_books_posts_nothing(self):
		book = self.book()
		frappe.set_user(CASHIER)
		self.assertEqual(opening_check.preview(json.dumps({"Cash": book})), [])
		doc = self.open(book)
		row = self.cash(doc)
		self.assertEqual((row.opening_check, flt(row.book_amount), flt(row.opening_difference), row.opening_entry), ("Matches", book, 0, None))
		self.assertEqual(self.notes(doc.name), [])

	def test_change_from_the_main_safe_is_posted_with_a_pin(self):
		book = self.book()
		frappe.set_user(CASHIER)
		found = opening_check.preview(json.dumps({"Cash": book + 50}))
		self.assertEqual([(f["mode_of_payment"], f["more"]) for f in found], [("Cash", True)])
		self.assertRaises(approval.ApprovalRequired, self.open, book + 50, "change")
		doc = self.open(book + 50, "change", MANAGER_USER, PIN)
		row = self.cash(doc)
		self.assertEqual((row.opening_check, flt(row.opening_difference)), ("Change from the Main Safe", 50))
		gl = {g.account: g for g in frappe.get_all("GL Entry", filters={"voucher_no": row.opening_entry, "is_cancelled": 0}, fields=["account", "debit", "credit"])}
		self.assertEqual((flt(gl[self.drawer].debit), flt(gl[self.safe].credit)), (50, 50))
		frappe.set_user("Administrator")
		self.assertAlmostEqual(self.book(), book + 50, 2)
		# Not a cash move of the shift: the opening already counts it, so it is expected once.
		closed = self.close()
		self.assertAlmostEqual(flt(closed["expected_amount"]), book + 50, 2)
		frappe.set_user("Administrator")
		row = self.cash(frappe.get_doc("Resto Shift", doc.name))
		self.assertEqual(row.opening_check, "Change from the Main Safe")  # kept through the close
		self.assertTrue(row.opening_entry)
		# What a closed shift counted stays: its opening entry cannot be cancelled alone.
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Journal Entry", row.opening_entry).cancel)

	def test_review_later_and_short_tell_the_manager(self):
		book = self.book()
		doc = self.open(book + 30, "review")
		row = self.cash(doc)
		self.assertEqual((row.opening_check, row.opening_entry), ("To Review", None))
		frappe.set_user("Administrator")
		self.assertEqual(len(self.notes(doc.name)), 1)
		self.assertAlmostEqual(self.book(), book, 2)  # nothing posted
		self.close()  # counted as expected: the 30 is still not in the books

		frappe.set_user("Administrator")
		book = self.book()
		frappe.set_user(CASHIER)
		found = opening_check.preview(json.dumps({"Cash": book - 20}))
		self.assertEqual([f["more"] for f in found], [False])
		doc = self.open(book - 20)
		row = self.cash(doc)
		self.assertEqual((row.opening_check, flt(row.opening_difference)), ("Short Since Last Close", -20))
		frappe.set_user("Administrator")
		self.assertEqual(len(self.notes(doc.name)), 1)

	def test_counting_blind_shows_no_books_figure(self):
		frappe.db.set_single_value("Resto Settings", "blind_shift_count", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		book = self.book()
		frappe.set_user(CASHIER)
		found = opening_check.preview(json.dumps({"Cash": book + 75}))
		self.assertEqual(found[0]["message"], "The drawer holds more than the books. Where did it come from?")

	def test_the_shift_summary_now_changes_nothing(self):
		# D-262: the figures so far, read only; counting blind, a cashier gets no expected amount.
		doc = self.open(self.book())
		before = frappe.db.get_value("Resto Shift", doc.name, ["status", "modified"], as_dict=True)
		rows_before = frappe.get_all("Resto Shift Payment", filters={"parent": doc.name}, fields=["name", "closing_amount", "expected_amount"], order_by="name")
		frappe.set_user(CASHIER)
		got = shift.shift_summary()
		self.assertEqual((got["shift"], got["live"], got["blind"]), (doc.name, 1, False))
		cash = next(r for r in got["rows"] if r["mode_of_payment"] == "Cash")
		self.assertIn("expected_amount", cash)
		self.assertIn("summary", got)
		frappe.set_user("Administrator")
		after = frappe.db.get_value("Resto Shift", doc.name, ["status", "modified"], as_dict=True)
		self.assertEqual((after.status, after.modified), ("Open", before.modified))
		self.assertEqual(
			frappe.get_all("Resto Shift Payment", filters={"parent": doc.name}, fields=["name", "closing_amount", "expected_amount"], order_by="name"), rows_before
		)

		frappe.db.set_single_value("Resto Settings", "blind_shift_count", 1)
		frappe.clear_document_cache("Resto Settings", "Resto Settings")
		frappe.set_user(CASHIER)
		got = shift.shift_summary()
		self.assertTrue(got["blind"])
		self.assertNotIn("summary", got)
		self.assertEqual({k for r in got["rows"] for k in r}, {"mode_of_payment"})
		# A manager sees all, counting blind or not.
		frappe.set_user(MANAGER_USER)
		with patch.object(shift, "_own_shift", return_value=frappe.get_doc("Resto Shift", doc.name)):
			got = shift.shift_summary()
		self.assertFalse(got["blind"])
		self.assertIn("expected_amount", got["rows"][0])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Resto Shift", doc.name, "status"), "Open")

	def test_a_shared_account_is_not_compared(self):
		with patch.object(opening_check, "_shared", return_value=True):
			doc = self.open(self.book() + 99)
		self.assertEqual(self.cash(doc).opening_check, "Not Checked")
		frappe.set_user("Administrator")
		self.assertEqual(self.notes(doc.name), [])
