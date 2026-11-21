# Clarifications (self-review, "grill me" round)

Each question was asked against the spec and answered in autonomous mode. Answers point to the
decision log (`docs/decisions.md`).

1. Where does an order live before payment, a draft invoice or its own document? → Own document (D-101).
2. Who submits the invoice, POSNext or Resto? → Resto, server side, standard fields (D-102).
3. How does POSNext's shift closing see Resto bills? → posa_pos_opening_shift (D-103).
4. What if no POSNext shift is open? → Refuse settling, with a clear message; setting can turn this off (D-103).
5. How are paid extras (extra cheese) billed and stocked? → Linked item lines (D-104).
6. Do we need courses? → Optional per line, with hold/fire (D-105).
7. One ticket per order or per station? → Per send per station (D-106).
8. How is a dish routed to a station? → Item, then group tree, then default (D-107).
9. React/Vue SPA or Desk pages? → Desk pages, plain JS (D-108).
10. What if realtime (socket.io) is down? → 15-second polling fallback (D-108).
11. Printer setup for the kitchen? → Network Printer Settings per station, background job (D-109).
12. Store table status or compute it? → Compute (D-110).
13. What happens to kitchen tickets when tables merge? → Lines move with their state (D-111).
14. Split equally into several invoices? → No, one invoice; split by lines makes several (D-112).
15. Who may give a big discount? → Resto Manager above the limit (D-113).
16. Is service charge a tax row or an item? → First tax row (D-114).
17. Is VAT computed on top of service? → Not in this version; VAT rows from the POS Profile template stay "On Net Total". Documented limitation (D-114).
18. Delivery drivers: users, employees or free text? → Employee link, optional (D-115).
19. How much to prep each day? → Par level minus kitchen stock (D-116).
20. Is waste a Stock Entry or its own document? → Own document posting a Stock Entry (D-117).
21. Which invoice fields do reports need? → resto_* custom fields (D-118).
22. Can a sent line's quantity change? → No, void and re-add (D-122).
23. Where are the tests run? → A separate MariaDB test site, test.resto (D-120).
24. Multi-branch now? → No (D-121).
25. Does POSNext's tax-inclusive switch break the service row? → It can; setup check warns (D-123).
26. When is stock checked? → At settlement only (D-124).
27. Can the waiter settle a bill? → Only with the Resto Cashier role.
28. Can a takeaway order be paid before cooking? → Yes, takeaway/delivery can be settled any time; the kitchen ticket continues.
29. What does the kitchen screen show for takeaway? → "Takeaway #<order number>" instead of a table.
30. What prints on the pre-bill? → Restaurant name, table, waiter, guests, lines with modifiers, service, taxes, total, per-person amount.
