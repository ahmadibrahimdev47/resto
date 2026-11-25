import frappe


def execute():
	"""D-216: the lock PIN moved out of pin_hash (now the approval PIN only) into lock_pin_hash. A row
	of a user who cannot approve held their lock PIN in pin_hash: it moves, so their lock keeps working
	and it never turns into an approval PIN if they are made a manager later. A manager's pin_hash stays
	their approval PIN; they set a different lock PIN."""
	from resto.resto.approval import is_approver

	for row in frappe.get_all(
		"Resto Manager PIN",
		filters={"pin_hash": ["is", "set"]},
		fields=["name", "user", "lock_pin_hash"],
	):
		if is_approver(row.user):
			continue
		values = {"pin_hash": None}
		if not row.lock_pin_hash:
			values["lock_pin_hash"] = frappe.db.get_value("Resto Manager PIN", row.name, "pin_hash")
		frappe.db.set_value("Resto Manager PIN", row.name, values, update_modified=False)
