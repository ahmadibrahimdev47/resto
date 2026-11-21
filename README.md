## Resto

Restaurant back-office app for ERPNext v16: recipes («وصفات»), daily prep, waste and food cost.
It runs next to POSNext, which stays the cashier screen.

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

### Design rules

- No overrides of ERPNext or POSNext classes or methods, and no monkey patches.
- Custom fields on standard doctypes use the `resto_` prefix.
- Prepared ingredients may go negative: the Item-level `allow_negative_stock` flag is set by the app. Raw materials stay strict.
