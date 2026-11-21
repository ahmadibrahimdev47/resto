## Resto

Restaurant app for ERPNext v16: floor and tables, orders, kitchen display, bills, reservations, recipes («وصفات»), daily prep, waste, food cost and restaurant reports.
It runs next to POSNext: floor bills become POS invoices in the cashier's POSNext shift, and POSNext stays the counter screen.

### Requirements

- Frappe v16 and ERPNext v16 only.

### Install

```bash
cd ~/frappe-bench
bench get-app <repo-url>
bench --site <site> install-app resto
bench --site <site> migrate
```

### What is in this version

- **Resto Settings** («إعدادات المطعم»): company, warehouses, item groups, selling price list and waste account.
- **Resto Recipe** («وصفة»), one per item, in two types:
  - **Prepared Ingredient** («مكوّن مجهّز»): on save, the app keeps a submitted default BOM in sync. It creates a new BOM only when the ingredients change.
  - **Menu Dish** («طبق منيو»): on save, the app keeps a flat Product Bundle in sync.
    - A dish can include another dish. Only the included dish's main ingredients are taken, not its "Served With" sides.
    - Every bundle component must be a stock item, because POSNext rejects the sale otherwise.
- **Recipe cost**: computed from current valuation rates. Prepared ingredients are costed from their own recipe.
- **Import**: creates recipes from existing default BOMs and Product Bundles.

- **Floor** («الصالة», /desk/resto-floor): halls and tables with live status, dine-in / takeaway / delivery orders, menu with modifiers («إضافات»), courses with hold and fire, kitchen notes, move and merge tables, discounts with role limits, service charge, pre-bill, pay with cash/card and change, split by items.
- **Kitchen display** («شاشة المطبخ»): tickets per station, statuses New / In Progress / Ready / Served, recall, void tickets, timers, automatic server printing per station.
- **Bills**: each payment creates a submitted POS Sales Invoice with stock taken through the recipe bundles, VAT and service from the POS Profile (VAT on service optional), linked to the open POSNext shift. The cashier pays the amount shown or nothing is billed; a retried click never bills twice. Only a manager cancels a floor bill, and the order is listed to bill again.
- **Reservations** («الحجوزات»): booking, table hold before the time, seat on arrival.
- **Daily prep** («التحضير اليومي»): par levels, suggested quantities, Manufacture entries from the recipe BOM, raw material transfer from the main store.
- **Waste** («الهالك»): reasons per line, Material Issue to the waste account at stock cost; dishes are expanded into their ingredients.
- **Analytics** («تحليلات المطعم») with KPIs and charts, and reports: Sales Analysis, Menu Engineering, Food Cost (recipe vs actual), Kitchen Times, Table Analysis (turns, RevPASH), Voids and Discounts, Waste Analysis, Customer Insights, Purchase Price Changes, Negative Prepared Stock.
- **Setup Check** («فحص الإعداد») in Resto Settings: lists what is missing or risky before service.
- Roles: Resto Manager, Waiter, Cashier, Kitchen, Host. Screens and prints are in Arabic (RTL) and English.

### Design rules

- No overrides of ERPNext or POSNext classes or methods, and no monkey patches.
- Custom fields on standard doctypes use the `resto_` prefix.
- Prepared ingredients may go negative: the Item-level `allow_negative_stock` flag is set by the app. Raw materials stay strict.
