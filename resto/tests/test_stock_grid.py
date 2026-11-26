"""A Stock Entry row's Difference Account is a column of the items grid (D-254).

bench --site test.resto run-tests --module resto.tests.test_stock_grid
"""

import frappe
from frappe.tests import IntegrationTestCase

from resto.setup import install


class TestStockRowGrid(IntegrationTestCase):
	def test_the_expense_account_is_a_grid_column_within_ten(self):
		install.show_stock_row_expense_account()
		install.show_stock_row_expense_account()  # every migrate: the same grid
		meta = frappe.get_meta(install.STOCK_ROW)
		df = meta.get_field("expense_account")
		self.assertEqual((df.in_list_view, df.columns), (1, 2))
		self.assertFalse(df.hidden)
		shown = [d for d in meta.fields if d.in_list_view]
		self.assertLessEqual(sum(install._grid_width(d) for d in shown), install.GRID_COLUMNS)
		# What the grid needs to issue stock stays in it.
		self.assertTrue({"item_code", "qty", "expense_account"} <= {d.fieldname for d in shown})
