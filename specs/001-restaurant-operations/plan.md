# Plan 001: Restaurant operations

## Architecture

```
Waiter tablet ──► Desk page "resto-floor" ──► resto.api.floor / resto.api.orders
Kitchen screen ─► Desk page "resto-kitchen" ─► resto.api.kitchen
Back office ────► Resto doctypes (forms and lists), reports
                        │
                        ▼
Resto Order ──send──► Resto Kitchen Ticket (per station) ──► server print (optional)
     │
     └─settle──► Sales Invoice (is_pos, update_stock, POS Profile, payments, resto_* fields,
                 posa_pos_opening_shift) ──► stock via Product Bundles of menu recipes
Resto Prep ──submit──► Stock Entry (Manufacture, per prepared recipe BOM)
Resto Waste ─submit──► Stock Entry (Material Issue to waste account)
```

Realtime: `frappe.publish_realtime("resto_update", {...}, after_commit=True)` on every order and
ticket change; pages also poll (settings `screen_refresh_seconds`, default 15).

## Data model (all in module "Resto")

### Masters
- **Resto Hall**: hall_name (name), sequence, grid_columns (default 6), is_active.
- **Resto Table**: table_name (name), hall, seats, shape (Square/Round/Rectangle),
  grid_row, grid_col, is_active.
- **Resto Kitchen Station**: station_name (name), is_default, is_active, printer
  (Network Printer Settings), print_format, items (Resto Station Item), item_groups
  (Resto Station Item Group).
- **Resto Modifier Group**: group_name (name), min_select, max_select (0 = no limit),
  options (Resto Modifier Option: option_name, item, price, is_default),
  applies_to (Resto Modifier Applies To: item_code or item_group).

### Transactions
- **Resto Order** (not submittable, status driven), naming series `ORD-.YY.MM.-.####`:
  order_type (Dine In/Takeaway/Delivery), status (Open/Bill Requested/Paid/Cancelled/Merged),
  company, pos_profile, price_list, table, hall, guests, waiter, opened_at, closed_at,
  customer, customer_name, phone, address, driver, delivery_status, reservation,
  items (Resto Order Item), discount_percentage, discount_amount, service_charge_percent,
  net_total, bill_service_amount, bill_tax_amount, bill_grand_total, notes,
  cancel_reason, merged_into.
- **Resto Order Item**: item_code, item_name, qty, rate, modifiers (JSON), modifiers_text,
  modifiers_amount, amount, notes, course, hold, station, kitchen_status
  (Not Sent/Sent/In Progress/Ready/Served/Void), sent_at, ticket, billed_qty, void_reason,
  voided_by.
- **Resto Kitchen Ticket**, naming series `KOT-.YY.MM.-.#####`: order, order_type, table,
  station, ticket_type (Order/Void), status (New/In Progress/Ready/Served/Cancelled),
  waiter, sent_at, started_at, ready_at, served_at, printed, items
  (Resto Kitchen Ticket Item: order_item, item_code, item_name, qty, modifiers_text, notes, course).
- **Resto Reservation**, naming series `RSV-.YY.-.####`: customer_name, phone, customer,
  reservation_date, reservation_time, guests, hall, table, status
  (Booked/Seated/Cancelled/No Show), notes, order.
- **Resto Prep** (submittable): posting_date, company, warehouse, source_warehouse, items
  (Resto Prep Item: recipe, item_code, uom, par_qty, current_qty, suggested_qty, qty,
  stock_entry).
- **Resto Waste** (submittable): posting_date, company, warehouse, items (Resto Waste Item:
  item_code, qty, uom, reason, note, valuation_rate, amount), total_amount, stock_entry.

### Changes to existing doctypes
- Resto Settings: pos_profile, require_pos_shift, service_charge_percent,
  service_charge_account, service_on_dine_in / _takeaway / _delivery, delivery_fee_item,
  delivery_fee, waiter_discount_limit, cashier_discount_limit, reservation_hold_minutes,
  screen_refresh_seconds.
- Resto Recipe: par_qty (prepared only).
- Sales Invoice custom fields: resto_order, resto_table, resto_waiter, resto_order_type,
  resto_guests (read only, in a collapsible "Restaurant" section).

## Server modules
- `resto/setup/install.py`: roles, custom fields (after_install, after_migrate).
- `resto/resto/routing.py`: station for item.
- `resto/resto/billing.py`: build, preview and submit invoices; POSNext shift lookup.
- `resto/api/floor.py`: floor state, menu, modifiers.
- `resto/api/orders.py`: create, add/update/remove lines, send, fire, void, move, merge,
  request bill, discount, settle, cancel, delivery status.
- `resto/api/kitchen.py`: tickets for a station, status changes.
- `resto/api/setup_check.py`: setup check.
- Reports (script reports): Resto Sales Analysis, Resto Menu Engineering, Resto Food Cost,
  Resto Kitchen Times, Resto Waste Analysis, Resto Negative Prepared Stock.

## Permissions
Server functions call `require_roles(...)`; documents carry DocPerms per role.
Manager-only: void, cancel, discounts above limit, merge.

## Pages
- `resto-floor`: halls tabs, table grid, takeaway/delivery list, order panel (menu, cart,
  modifiers dialog, send, fire, void, bill, settle dialog with payments and split).
- `resto-kitchen`: station picker, ticket cards with timers, start/ready/served, recall.
- `resto-setup-check`: checklist.

## Tests (on test.resto only)
Orders and totals, routing, send/void tickets, move/merge, billing with service and discount,
split by lines, POSNext shift link, prep stock entries, waste stock entries.

## Deploy
Commit → push → `git pull` on the VM → `bench --site erp.local migrate` → user restarts
(`sudo supervisorctl restart all`) when hooks or Python modules change.
