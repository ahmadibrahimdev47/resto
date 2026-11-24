import frappe

# Words of a copy kept by the restaurant (it shows the sides changed, as the kitchen ticket does).
RESTAURANT_WORDS = ("مطعم", "المطعم", "مطبخ", "كاونتر", "الإدارة", "restaurant", "kitchen", "office", "counter")
CUSTOMER_WORDS = ("عميل", "العميل", "زبون", "customer", "guest")


def execute():
	"""Bill and receipt copies labelled for the restaurant show the side dishes; the others do not."""
	for row in frappe.get_all(
		"Resto Print Copy",
		filters={"parenttype": "Resto Settings", "parentfield": ("in", ("bill_copies", "receipt_copies"))},
		fields=["name", "label"],
	):
		label = (row.label or "").lower()
		show = any(w in label for w in RESTAURANT_WORDS) and not any(w in label for w in CUSTOMER_WORDS)
		frappe.db.set_value("Resto Print Copy", row.name, "show_sides", 1 if show else 0, update_modified=False)
