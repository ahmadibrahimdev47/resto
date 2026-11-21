# Decision log

Decisions taken while building Resto. Each has an id, the decision and the reason.
"Auto" decisions were taken in autonomous mode (the product owner asked the builder to decide
alone and report later); they can be revisited.

## Step 1 (recipes)

| Id | Decision | Reason |
|---|---|---|
| D-001 | ERPNext/Frappe v16 only. | Product owner rule. |
| D-002 | Prepared ingredient recipe → default submitted BOM; menu dish recipe → flat Product Bundle. | Reuse ERPNext manufacturing and POS stock deduction. |
| D-003 | A dish inside a dish adds only its main ingredients, not its sides. | Avoid duplicated sides (rice, bread) in combos. |
| D-004 | Negative stock only for prepared ingredients (Item flag); raw materials stay strict. | Product owner decision. |
| D-005 | No overrides or monkey patches of ERPNext or POSNext. | Upgrade safety; POSNext already overrides Sales Invoice. |

## Spec 001 (restaurant operations), auto decisions

| Id | Decision | Reason |
|---|---|---|
| D-101 | A dine-in/takeaway/delivery order is a Resto Order document until it is paid; the Sales Invoice is created only at settlement. | Orders need kitchen state per line, courses, voids and transfers that a draft invoice cannot hold cleanly. |
| D-102 | Resto creates the invoice itself on the server: Sales Invoice, is_pos=1, update_stock=1, POS Profile and payments, then submits it. | Standard ERPNext; stock goes through the recipe bundles exactly like POSNext sales. |
| D-103 | When POSNext is installed, the invoice gets `posa_pos_opening_shift` = the cashier's open POSNext shift for that POS Profile, and settling is refused without an open shift (setting, on by default). | POSNext shift closing collects invoices by that field, so cash counts stay correct. |
| D-104 | Modifiers are Resto Modifier Groups (min/max choices) applied to items or item groups. A priced option must link to an item and becomes its own invoice line; a free option is only a kitchen note. | Every amount on an invoice needs an item; stock for paid extras is deducted. |
| D-105 | Courses per line: Starter, Main, Dessert, Drinks (or none). A held line is not sent until its course is fired. | Common fine-dining flow, optional for casual places. |
| D-106 | Kitchen ticket = one Resto Kitchen Ticket per send per station, with its own status (New, In Progress, Ready, Served, Cancelled) and type (Order, Void). | Stations work independently; timings per station feed the kitchen-time report. |
| D-107 | Routing: item → station by item list, else by item group walking up the tree, else the default station. | Simple setup: most places route by group. |
| D-108 | Kitchen display and floor are Desk pages in plain JS, refreshed by Frappe realtime and by polling every 15 s. | No extra build tooling; realtime can fail on LAN installs (seen on the trial). |
| D-109 | Kitchen printing uses Frappe's server printing (Network Printer Settings) when a station has a printer, in a background job; failures are logged, never block the order. | No desktop agent needed for the kitchen; QZ Tray stays a POSNext option for receipts. |
| D-110 | Table status is computed (open order / bill requested / reservation window), not stored. | Never gets out of sync. |
| D-111 | Merge = move all lines of order B into order A (keeping kitchen state) and close B as "Merged". Move = change the order's table. | Kitchen history stays intact. |
| D-112 | Split by lines = several invoices from one order (billed quantity tracked per line). "Split equally" = one invoice with the per-person amount shown on the pre-bill. | Fractional bundle quantities in several invoices would distort stock. |
| D-113 | Discounts: order-level percent or amount. Above the limit in settings the user must hold Resto Manager. | Server-side control; an approval workflow can come later. |
| D-114 | Service charge: percent in settings, per order type, posted to a service account. If the POS Profile tax template already has a row on that account (for example VAT on Previous Row Total after it), Resto sets that row's rate; otherwise it adds an On Net Total row at the end. | Egyptian restaurants usually charge 12% service on dine-in. |
| D-115 | Delivery: customer, phone, address, driver (Employee, optional) and a delivery fee item from settings; statuses Out for Delivery / Delivered; paid when settled. | Covers phone-order delivery without aggregator integration. |
| D-116 | Daily prep suggestion = par level of the prepared recipe − current kitchen stock (never below zero). | Transparent and standard; sales-based forecasting can come later. |
| D-117 | Waste is its own submittable document that posts a Material Issue to the waste account with a reason per line. | Reasons feed the waste report; accountants get one clear journal. |
| D-118 | Invoices carry custom fields resto_order, resto_table, resto_waiter, resto_order_type, resto_guests (Sales Invoice) for reports. | Reports need them; FR-6. |
| D-119 | Roles Resto Manager / Waiter / Cashier / Kitchen / Host are created by the app on install and migrate. | Role-based server checks (FR-9). |
| D-120 | Tests run on a separate MariaDB test site (`test.resto`) created with a dedicated test DB user, never on a real site. A SQLite site was tried first, but Frappe v16 failed to install on SQLite (Module Def unique error). | Real data stays untouched. |
| D-121 | One branch per site for now; orders still carry company and POS Profile so branches can come later. | Keeps the first version simple. |
| D-122 | A sent line cannot be edited; it is voided (with reason) and re-added. | Kitchen and audit trail stay correct. |
| D-123 | If the POSNext POS Settings for the profile marks taxes as inclusive, the setup check warns, because POSNext would also mark Resto's service row inclusive. | POSNext's Sales Invoice validate hook rewrites tax rows. |
| D-124 | Stock shortages are only checked when the bill is settled (raw materials strict, prepared allowed negative). The setup check lists dishes whose components are short in the kitchen. | Ordering must never be blocked mid-service; the warning comes early. |
| D-125 | Prices always come from the settings price list on the server; rates sent by a browser are ignored, and the invoice uses ignore_pricing_rule so ERPNext pricing rules cannot change a floor price. | The bill must match the menu exactly; no client tampering. |
| D-126 | Fields the server owns (status, times, bill details, kitchen status, billed qty, void data) are restored from the saved order unless Resto's own API changes them. | Desk form edits cannot break the order state. |
| D-127 | Split bills: each part gets its share of a fixed discount; the last part gets the remainder, so the parts add up to the order discount to the piaster. | Rounding per part would otherwise lose or add money. |
| D-128 | The delivery fee is billed on the first invoice of the order only. | Never charged twice on split bills. |
| D-129 | Cancelling a floor invoice in ERPNext reopens its order and gives back the billed quantities, so the items can be billed again. | Corrections stay possible without breaking the order history. |
| D-130 | The POSNext packed-item keying patch is skipped on ERPNext versions whose add_packed_item_row already takes linked_packed_item (v16 since Nov 2026). This is a one-line local fix in POSNext on the trial server, not part of Resto; it should go upstream. | Without it every bundle (recipe) sale crashes on this ERPNext version, from POSNext and from Resto alike. |
| D-131 | Card and bank payments cannot exceed the bill; only cash can give change. Payments must cover the bill. | Change is only ever paid in cash; card overpayment would break the shift totals. |
| D-132 | A dine-in bill can only include lines already sent to the kitchen. Takeaway and delivery lines are sent automatically when settled. | Nothing is billed that the kitchen never saw; counter orders stay one step. |
| D-133 | Daily prep posts one Manufacture stock entry per prepared recipe from its BOM inside the kitchen warehouse. A button creates a draft Material Transfer from the main store for missing raw materials. | Prepared stock and its cost come from the real ingredients; the store keeper reviews the transfer before submitting. |
| D-134 | Waste of a menu dish issues its recipe components; the cost in the waste document is rewritten from the stock ledger after submit. | A dish is not a stock item; the waste value must equal what left the stock. |
| D-135 | Food cost reports show two numbers: recipe (theoretical) cost at today's recipe costs, and actual cost posted by the stock ledger at the time of sale. Total food cost adds waste and stock count differences. | Standard restaurant cost control: theoretical vs actual variance. |
| D-136 | Menu engineering uses Kasavana-Smith: popularity line = 70% of the fair share (1/N), margin line = weighted average contribution margin. Classes Star, Plowhorse, Puzzle, Dog with advice. | The common method used by restaurant consultants. |
| D-137 | RevPASH = net sales / (seats x opening hours x days), with opening hours as a report filter. | No opening-hours master yet; the filter keeps it honest. |
| D-138 | An analytics page (KPIs and charts for a period) links to ten detailed script reports. | Same kind of screen as URY and big restaurant systems, using only Desk tools. |
| D-139 | Orders cannot be merged when part of one is paid or when they bill through different POS Profiles. | Paid invoices and shift totals must not move. |
| D-140 | Discount limits by role: waiter 0%, cashier 10% by default; above that needs Resto Manager. A discount needs a reason. | Discounts are the main leak in restaurants; reasons feed the voids and discounts report. |
| D-141 | A setup check in Resto Settings lists errors and warnings: missing settings, POS Profile that does not update stock, payment accounts, inclusive taxes (template or POSNext), price list mismatch, stations, users per role, menu items without price or recipe, prepared recipes without BOM, zero-cost recipes, and kitchen ingredients out of stock. | Problems show before service, not at the cashier. |
| D-142 | Every screen and print follows the user language direction: RTL for Arabic. Pages use logical CSS (start/end), the back arrow flips, and the kitchen ticket and pre-bill set dir from the print language. | The restaurant works in Arabic; English users still get LTR. |
| D-143 | Setting "VAT on Service" (on by default): the service row is put first and the VAT rows are charged on the previous row total, so VAT is also charged on the service charge. Off: VAT on food only. | Egyptian VAT is charged on the service charge; the setting covers places that treat it differently. |
| D-144 | Inclusive taxes follow POSNext's POS Settings exactly (replaces D-123). Inclusive prices with a service charge are refused, because ERPNext cannot add a charge on top of an inclusive price. With inclusive taxes the discount applies to the grand total. | One rule in both apps; no silent wrong VAT. |
| D-145 | Settling sends the amount the cashier saw and a request id. If the payable changed meanwhile, nothing is billed; a retried click (also after the order closed) returns the same invoice. After insert the payable is checked again. | No double charge and no bill that differs from the screen. |
| D-146 | When a paid floor invoice is cancelled after the guests left, its order is marked "to bill again" and listed apart; the table stays free for new guests. | A correction must not block a table. |
| D-147 | Only Resto Manager cancels a floor invoice; an amended copy of a floor invoice is refused (bill again from the order); a POS closing shift must list every floor invoice of the shift. | Cash control: every bill is in exactly one shift. |
| D-148 | A discount given after part of the order is paid only counts the unbilled part, and the role limit is checked in money. | Limits cannot be beaten by splitting the bill. |
| D-149 | Any change to a bill-requested order (lines, discount, service, fee, customer) sends it back to Open; the printed bill must be printed again. | The guest never pays a different amount than the printed bill. |
| D-150 | An order closes as Paid on its own when nothing unbilled is left (for example the rest was voided). Paid closes its kitchen tickets; Cancelled cancels open ones. | No orders or tickets left hanging. |
| D-151 | Voiding a dish that was already cooked can post its ingredients (and paid modifiers) as waste; on by default once the kitchen started it. | Food cost stays exact; the cooked food really left the stock. |
| D-152 | Waste, prep and count costs in reports come from the stock ledger. Food cost % = (COGS + waste + prep adjustments − count differences) ÷ food sales (delivery fee left out). Opening stock is not a count difference. | Matches the accounts to the piaster. |
| D-153 | Menu engineering leaves out modifier items and includes priced menu items that sold nothing (as zero). | Dogs that never sell must show up; extras are not dishes. |
| D-154 | Recipe costs are refreshed every day by a scheduled job (also from the button). | Purchase price changes reach the menu cost without manual work. |
| D-155 | On install Resto grants read access its roles need (Item, Customer, Employee, Sales Invoice, Bin, Purchase Receipt) through Custom DocPerm. The first custom grant on a doctype copies its standard rules, as Frappe always does. | Floor and kitchen users work without System Manager. |
| D-156 | Kitchen tickets move only along allowed steps (New → In Progress → Ready → Served, recall back to New). A void strikes the item on its original ticket; a ticket with nothing left is cancelled. Ready tickets leave the board after two hours. | Two screens cannot move a ticket backwards; the cook sees what not to make. |
| D-157 | Indexes on order status, table and time, line kitchen status, and ticket status, station, order and time. | The floor and kitchen screens poll every few seconds. |
| D-158 | An invoice discount is spread by ERPNext over all rows, the delivery fee row too. Kept as ERPNext does it. | The ledger stays exact; only the split per row differs. |
| D-159 | The hall map keeps its physical layout in Arabic (column 1 on the left); text inside each table card is RTL. | The map is a picture of the room, not text. |
| D-160 | The floor shows Pay only to cashiers and managers, and voiding a sent item only to managers (the server checks too). | Fewer dead buttons for waiters. |
| D-161 | Two merges of the same orders lock them in name order. | No deadlock at a busy cashier. |
| D-162 | The kitchen screen polls on a fixed timer from the start and uses one sound context, unlocked on the first touch. | Browsers block sound until the screen is touched; the beep must not go silent. |
