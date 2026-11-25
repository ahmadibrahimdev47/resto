from frappe.model.document import Document


class RestoConsumableItem(Document):
	"""A stock item every order of the ticked types takes, per order or per guest (Resto Settings, D-224)."""
