# Tasks 001

Legend: [x] done, [ ] to do.

## Phase A: foundation
- [x] A1 Settings fields (POS Profile, service, delivery, discount limits, refresh).
- [x] A2 Install/migrate hooks: roles and Sales Invoice custom fields.
- [x] A3 Recipe par_qty.
- [x] A4 Masters: Hall, Table, Kitchen Station (+2 child tables), Modifier Group (+2 child tables).

## Phase B: orders and kitchen
- [x] B1 Resto Order + Resto Order Item doctypes and controller (totals, validations).
- [x] B2 Routing helper.
- [x] B3 Resto Kitchen Ticket + item, send/void/fire logic, realtime, server printing.
- [x] B4 Move, merge, cancel, delivery status.
- [x] B5 Kitchen ticket print format.

## Phase C: billing
- [x] C1 Bill preview (service, taxes, discount) and request bill.
- [x] C2 Settle: invoice build/submit, payments, split by lines, POSNext shift link.
- [x] C3 Pre-bill print format.

## Phase D: screens
- [x] D1 resto-floor page.
- [x] D2 resto-kitchen page.
- [x] D3 Sidebar links and translations.

## Phase E: reservations, prep, waste
- [x] E1 Resto Reservation + seat action.
- [x] E2 Resto Prep (suggestions, manufacture entries, raw transfer).
- [x] E3 Resto Waste (material issue).

## Phase F: reports and setup check
- [x] F1 Reports (sales, menu engineering, food cost, kitchen times, waste, negative prepared).
- [x] F2 Setup check page.

## Phase G: quality
- [x] G1 Tests on test.resto.
- [ ] G2 Deploy to the production site (schema only, no data) and smoke test screens.
- [x] G3 README and decision log update.
