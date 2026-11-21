# Spec 001: Restaurant operations

Status: approved for build (autonomous mode, see `docs/decisions.md`)

## Goal

Turn Resto from a recipe tool into a full restaurant back- and front-of-house app on
ERPNext v16, next to POSNext:

- floors, halls and tables with a live floor plan;
- dine-in, takeaway and delivery orders taken by waiters on a tablet;
- kitchen tickets routed to kitchen stations, a kitchen display and kitchen printing;
- bills with service charge, discounts with limits, split and merged bills;
- reservations;
- daily prep, waste and food-cost reporting on top of the existing recipes.

POSNext stays the fast counter till (and the offline fallback). Resto never changes POSNext
code and talks to it only through standard ERPNext documents.

## Users and roles

| Role | Arabic | Does |
|---|---|---|
| Resto Manager | مدير المطعم | Setup, voids, discounts above the waiter limit, reports |
| Resto Waiter | ويتر | Opens tables, takes orders, sends to kitchen, asks for the bill |
| Resto Cashier | كاشير | Settles bills, takeaway and delivery orders, prints bills |
| Resto Kitchen | مطبخ | Sees tickets of his station, marks them in progress / ready |
| Resto Host | استقبال | Reservations and seating |

A user may hold several roles. System Manager can do everything.

## User stories

### Floor and tables
1. As a manager I define halls (صالات) and their tables (ترابيزات) with seats and a position
   on a simple grid, so the floor screen looks like the restaurant.
2. As a waiter I open the floor screen and see every table coloured by status: free, occupied,
   bill requested, reserved soon. Occupied tables show guests, minutes seated and amount.
3. As a waiter I tap a free table, enter the number of guests and start an order.
4. As a waiter I move an order to another table, or merge two tables into one bill.

### Ordering
5. As a waiter I add menu items by group or search, with quantity, notes and modifiers
   (e.g. doneness, extra cheese with a price).
6. As a waiter I mark lines with a course (starter, main, dessert) and "hold" a course, then
   "fire" it later.
7. As a waiter I press "Send to kitchen"; only lines not yet sent are sent, each to the
   kitchen station that makes it.
8. As a waiter I void a sent line with a reason; the station gets a void ticket. Voiding needs
   the Resto Manager role (or a manager approves on the same device).
9. As a cashier I take takeaway and delivery orders on the same order screen; delivery orders
   carry customer, phone, address, driver and a delivery fee.

### Kitchen
10. As a manager I define kitchen stations (grill, fryer, cold, drinks…) and which item groups
    or items each one makes; one station is the default.
11. As kitchen staff I see my station's tickets on a kitchen display, oldest first, with the
    time since sent, notes and modifiers; I mark a ticket "in progress" and "ready".
12. As a station with a printer I get the ticket printed automatically (network printer through
    Frappe's Network Printer Settings), or I print it from the display.
13. As a waiter I see which of my orders have ready items, so I can serve them.

### Bill and payment
14. As a waiter I print a pre-bill (الشيك) for the table; the table turns "bill requested".
15. As a cashier I settle the bill with one or more payment modes; Resto creates and submits a
    standard POS Sales Invoice (stock is deducted through the recipes' bundles) and frees the
    table.
16. As a cashier I apply a discount (percent or amount); above the configured limit a manager
    must approve.
17. The bill adds a service charge (percent from settings, optional per order type) and the
    POS Profile taxes.
18. As a cashier I split a bill by lines or into equal parts; each part becomes its own invoice.
19. Bills settled in Resto land in the open POSNext shift of the same POS Profile, so the shift
    closing counts them.

### Reservations
20. As a host I record a reservation (name, phone, date, time, guests, hall/table, notes).
21. The floor shows a table as "reserved" from N minutes before the time; seating the
    reservation opens an order on that table.

### Back office (existing MVP scope, completed here)
22. Recipes (done in step 1).
23. Daily prep: the app suggests quantities of prepared ingredients, the manager confirms, and
    the app posts Manufacture stock entries in the kitchen warehouse.
24. Kitchen transfer: move raw materials from the main store to the kitchen.
25. Waste: record waste by item and reason; the app posts a Material Issue to the waste account.
26. Reports: theoretical food cost, menu engineering, sales by hour / table / waiter / order type,
    kitchen times, waste, negative prepared stock.
27. Setup check: one page that lists what is missing (warehouses, price list, POS Profile,
    stations, roles…).

## Functional requirements

- FR-1 Every restaurant document is per company and carries the POS Profile it bills through.
- FR-2 An order is never deleted after something was sent to the kitchen; it can only be
  cancelled with a reason (manager).
- FR-3 Prices come from the settings price list (Item Price), modifiers add their own price.
- FR-4 Kitchen routing: item → station by item, else by item group (walking up the tree),
  else the default station.
- FR-5 The floor and kitchen screens refresh in real time (Frappe realtime) and also poll every
  15 seconds, because realtime may be unavailable on a LAN install.
- FR-6 The invoice uses standard ERPNext fields only, plus `resto_order` and `resto_table`
  custom fields (prefixed `resto_`).
- FR-7 All screens are Arabic through translations, right-to-left, and usable on a 10" tablet.
- FR-8 Nothing in Resto overrides an ERPNext or POSNext class or method.
- FR-9 Permissions are role based; the server checks every action, not only the screen.

## Out of scope (this spec)

- Egyptian e-invoice / e-receipt (ETA) integration.
- Online aggregators (Talabat, Elmenus…) and a customer self-ordering QR menu.
- Loyalty, wallet and coupons (POSNext has them).
- Inventory counting (ERPNext Stock Reconciliation covers it).
- Offline mode for Resto screens (POSNext is the offline till).

## Success criteria

- A waiter can open a table, order, send, get a bill and the cashier can settle it in under a
  minute on a tablet.
- Every settled bill appears as a submitted POS Sales Invoice with correct stock movements.
- A kitchen station sees a new ticket within 15 seconds of sending, without reloading.
- Automated tests cover orders, routing, billing, split, merge, prep and waste.
