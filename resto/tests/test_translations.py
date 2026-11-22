"""Resto's Arabic wins over apps installed after it. Run only on a throw-away test site:

bench --site test.resto run-tests --module resto.tests.test_translations
"""

import frappe
from frappe.tests import IntegrationTestCase

from resto.setup import translations
from resto.tests import fixtures as fx


class TestTranslationOverrides(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.guard()

	def test_resto_wording_wins(self):
		translations.sync()
		translations.sync()  # twice: nothing new on the second run
		from frappe.translate import get_all_translations

		merged = get_all_translations("ar")
		mine = {translations.key(s, c): t for s, c, t in translations.resto_rows()}
		self.assertEqual(merged.get("Submit"), mine["Submit"])
		written = frappe.parse_json(frappe.db.get_global(translations.WRITTEN) or "{}")
		for k, (source, context, text) in written.items():
			self.assertEqual(merged.get(k), text, k)
			self.assertEqual(len(frappe.get_all("Translation", filters={"language": "ar", "source_text": source,
				"context": context or ("is", "not set")})), 1, k)

	def test_user_record_is_left_alone(self):
		translations.sync()
		written = frappe.parse_json(frappe.db.get_global(translations.WRITTEN) or "{}")
		if not written:
			self.skipTest("No app installed after Resto disagrees with it on this site")
		k, (source, context, _text) = next(iter(written.items()))
		row = translations.find(source, context)
		frappe.db.set_value("Translation", row.name, "translated_text", "USER WORDING")
		translations.sync()
		self.assertEqual(frappe.db.get_value("Translation", row.name, "translated_text"), "USER WORDING")
		self.assertNotIn(k, frappe.parse_json(frappe.db.get_global(translations.WRITTEN) or "{}"))
		frappe.delete_doc("Translation", row.name, force=True)
		translations.sync()  # Resto's record comes back
		self.assertIn(k, frappe.parse_json(frappe.db.get_global(translations.WRITTEN) or "{}"))
