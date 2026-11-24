// Restaurant floor: halls and tables, takeaway/delivery orders, order taking and settling.
// Plain JS on purpose (no build step); every action is checked again on the server.

const API = "resto.resto.floor.";
const COURSES = ["", "Starter", "Main Course", "Dessert", "Drinks"];

frappe.pages["resto-floor"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Restaurant Floor"), single_column: true });
	wrapper.resto_floor = new RestoFloor(page, wrapper);
};

frappe.pages["resto-floor"].on_page_show = function (wrapper) {
	wrapper.resto_floor && wrapper.resto_floor.refresh();
};

function call(method, args, freeze) {
	return new Promise((resolve, reject) => {
		frappe.call({
			method: API + method,
			args: args || {},
			freeze: !!freeze,
			callback: (r) => resolve(r.message),
			error: (e) => reject(e),
		});
	});
}

// "9:05:00" (a time from the server) as "09:05".
const hhmm = (t) => {
	const [h, m] = String(t || "").split(":");
	return m === undefined ? "" : `${h.padStart(2, "0")}:${m}`;
};
const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
const money = (v) => format_currency(v || 0);

class RestoFloor {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.hall = localStorage.getItem("resto_floor_hall") || null;
		this.floor = null;
		this.menu = null;
		this.order = null;
		this.make();
		this.refresh();
		frappe.realtime.on("resto_update", () => this.schedule_refresh());
	}

	make() {
		this.page.set_primary_action(__("New Takeaway"), () => this.new_order("Takeaway"), "add");
		this.page.add_inner_button(__("New Delivery"), () => this.new_order("Delivery"));
		this.page.add_inner_button(__("Reservations"), () => frappe.set_route("List", "Resto Reservation"));
		this.page.add_inner_button(__("Kitchen Display"), () => frappe.set_route("resto-kitchen"));
		this.page.add_inner_button(__("Refresh"), () => this.refresh());

		$(this.page.body).html(`
			<style>${RestoFloor.css()}</style>
			<div class="resto-floor">
				<div class="rf-tabs"></div>
				<div class="rf-body"></div>
			</div>
			<div class="rf-order" style="display:none"></div>`);
		this.$tabs = $(this.page.body).find(".rf-tabs");
		this.$body = $(this.page.body).find(".rf-body");
		this.$order = $(this.page.body).find(".rf-order");
	}

	schedule_refresh() {
		clearTimeout(this._t);
		this._t = setTimeout(() => this.refresh(), 400);
	}

	async refresh() {
		if (!$(this.wrapper).is(":visible")) return;
		try {
			this.floor = await call("get_floor");
		} catch (e) {
			return;
		}
		if (!this._receipts_btn && this.floor.roles.cashier) {
			this._receipts_btn = this.page.add_inner_button(__("Receipts"), () => this.receipts());
		}
		this.render_floor();
		if (this.order) this.reload_lines(this.order.name);
		clearInterval(this._poll);
		this._poll = setInterval(() => this.refresh(), (this.floor.refresh_seconds || 15) * 1000);
	}

	// ------------------------------------------------------------------ printing

	kitchen_print_alert() {
		// Kitchen printing that stopped is shown here too: the kitchen may not be looking at a screen.
		const k = this.floor.kitchen_print || {};
		const parts = [];
		if (k.unprinted) parts.push(__("Kitchen tickets not printed: {0}", [k.unprinted]));
		if ((k.no_screen || []).length) parts.push(__("No kitchen screen is printing for: {0}", [esc(k.no_screen.join("، "))]));
		return parts.length ? `<div class="rf-print-alert">⚠ ${parts.join(" · ")}</div>` : "";
	}

	prints_here() {
		// Bills and receipts print from the browser only on the device that prints them (the cashier's),
		// not on a waiter's tablet. Kept per device; a cashier's device prints by default.
		let v = null;
		try {
			v = localStorage.getItem("resto_prints_here");
		} catch (e) {
			v = null;
		}
		return v === null ? !!(this.floor && this.floor.roles.cashier) : v === "1";
	}

	async receipts() {
		const rows = await frappe.xcall("resto.resto.printing.recent_receipts");
		const d = new frappe.ui.Dialog({ title: __("Receipts"), size: "large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		const here = this.prints_here();
		const list = (rows || [])
			.map(
				(r, i) => `<tr><td>${esc(hhmm(r.posting_time))}</td>
					<td><a href="/app/sales-invoice/${esc(r.name)}">${esc(r.name)}</a>${r.is_return ? " · " + __("Return") : ""}</td>
					<td>${r.resto_table ? __("Table {0}", [esc(r.resto_table)], "Resto") : esc(__(r.resto_order_type || "", null, "Resto"))} · ${esc(r.resto_order || "")}</td>
					<td class="rf-amt">${money(r.rounded_total || r.grand_total)}</td>
					<td>${r.resto_receipt_printed ? "🖨" : ""}</td>
					<td><button class="btn btn-default btn-xs" data-i="${i}">${__("Print Again")}</button></td></tr>`
			)
			.join("");
		d.fields_dict.html.$wrapper.html(`
			<p><button class="btn btn-sm ${here ? "btn-success" : "btn-default"}" data-here="1">${here ? "☑" : "☐"} ${__("This device prints bills and receipts")}</button></p>
			${list ? `<table class="table table-condensed">${list}</table>` : `<p class="text-muted">${__("No receipts today")}</p>`}`);
		d.fields_dict.html.$wrapper.find("[data-here]").on("click", () => {
			try {
				localStorage.setItem("resto_prints_here", here ? "0" : "1");
			} catch (e) {
				// private window: the default stays
			}
			d.hide();
			this.receipts();
		});
		d.fields_dict.html.$wrapper.find("[data-i]").on("click", async (e) => {
			const btn = $(e.currentTarget).prop("disabled", true);
			const r = rows[btn.attr("data-i")];
			await this.print_receipt(r.name, this.floor.receipt_mode || this.receipt_mode);
			btn.prop("disabled", false);
		});
		d.show();
	}

	async print_receipt(sales_invoice, mode) {
		mode = mode || this.receipt_mode;
		if (mode === "server") {
			await frappe.xcall("resto.resto.printing.reprint", { doctype: "Sales Invoice", name: sales_invoice });
			return frappe.show_alert({ message: __("Sent to printer"), indicator: "green" });
		}
		frappe.show_alert({ message: __("Printing…"), indicator: "blue" });
		const pages = await frappe.xcall("resto.resto.printing.receipt_pages", { sales_invoice });
		if (await resto.print_pages(pages)) {
			await frappe.xcall("resto.resto.printing.receipt_printed", { sales_invoice });
		}
	}

	// ------------------------------------------------------------------ floor

	render_floor() {
		const halls = this.floor.halls;
		if (!this.hall || !(this.hall === "__orders" || halls.find((h) => h.name === this.hall))) {
			this.hall = halls.length ? halls[0].name : "__orders";
		}
		const tabs = halls.map(
			(h) => {
				const busy = h.tables.filter((t) => t.status !== "free").length;
				return `<button class="btn btn-sm ${h.name === this.hall ? "btn-primary" : "btn-default"}" data-hall="${esc(h.name)}">
					${esc(h.name)} <span class="rf-count">${busy}/${h.tables.length}</span></button>`;
			}
		);
		tabs.push(
			`<button class="btn btn-sm ${this.hall === "__orders" ? "btn-primary" : "btn-default"}" data-hall="__orders">
				${__("Takeaway & Delivery")} <span class="rf-count">${this.floor.orders.length}</span></button>`
		);
		this.$tabs.html(this.shift_bar() + this.kitchen_print_alert() + tabs.join(""));
		this.$tabs.find("[data-shift]").on("click", (e) => {
			const act = $(e.currentTarget).attr("data-shift");
			if (act === "open") this.open_shift();
			else if (act === "expense") this.drawer_expense();
			else this.close_shift();
		});
		this.$tabs.find("button[data-hall]").on("click", (e) => {
			this.hall = $(e.currentTarget).attr("data-hall");
			localStorage.setItem("resto_floor_hall", this.hall);
			this.render_floor();
		});

		if (this.hall === "__orders") return this.render_orders();
		const hall = halls.find((h) => h.name === this.hall);
		if (!hall.tables.length) {
			this.$body.html(`<p class="text-muted">${__("No tables in this hall yet.")}</p>`);
			return;
		}
		const cards = hall.tables.map((t) => this.table_card(t));
		this.$body.html(
			`<div class="rf-grid" dir="ltr" style="grid-template-columns: repeat(${hall.grid_columns}, minmax(120px, 1fr))">${cards.join("")}</div>
			<div class="rf-legend">
				<span class="rf-dot free"></span>${__("Free", null, "Resto")}
				<span class="rf-dot occupied"></span>${__("Occupied")}
				<span class="rf-dot bill"></span>${__("Bill Requested")}
				<span class="rf-dot reserved"></span>${__("Reserved")}
			</div>`
		);
		this.$body.find(".rf-table").on("click", (e) => {
			const name = $(e.currentTarget).attr("data-table");
			this.open_table(hall.tables.find((t) => t.name === name));
		});
	}

	table_card(t) {
		const o = t.order;
		const pos =
			t.grid_row && t.grid_col ? `grid-row:${t.grid_row};grid-column:${t.grid_col};` : "";
		let info = `<div class="rf-seats">${__("{0} seats", [t.seats || 0])}</div>`;
		if (o) {
			info = `<div>${__("{0} guests", [o.guests || 0])} · ${o.minutes}′</div>
				<div class="rf-amount">${money(o.bill_grand_total || o.net_total)}</div>
				<div class="rf-waiter">${esc(o.waiter_name)}</div>
				${o.ready_count ? `<span class="rf-ready">${__("Ready")} ${o.ready_count}</span>` : ""}`;
		} else if (t.reservation) {
			info = `<div>${esc(t.reservation.customer_name)}</div>
				<div>${esc((t.reservation.reservation_time || "").slice(0, 5))} · ${__("{0} guests", [t.reservation.guests])}</div>`;
		}
		return `<div class="rf-table ${t.status} ${t.shape === "Round" ? "round" : ""}" data-table="${esc(t.name)}" dir="${frappe.utils.is_rtl() ? "rtl" : "ltr"}" style="${pos}">
			<div class="rf-name">${esc(t.name)}</div>${info}</div>`;
	}

	render_orders() {
		const orders = this.floor.orders;
		if (!orders.length) {
			this.$body.html(`<p class="text-muted">${__("No open takeaway or delivery orders.")}</p>`);
			return;
		}
		this.$body.html(
			`<div class="rf-list">${orders
				.map(
					(o) => `<div class="rf-ocard ${o.status === "Bill Requested" ? "bill" : "occupied"}" data-order="${esc(o.name)}">
					<div class="rf-name">${__(o.order_type, null, "Resto")} · ${esc(o.name)}</div>
					${o.rebill ? `<div class="text-danger">${__("To Bill Again")} · ${__("Table {0}", [esc(o.resto_table)], "Resto")}</div>` : ""}
					<div>${esc(o.customer_name || "")} ${esc(o.phone || "")}</div>
					<div>${o.minutes}′ · <b>${money(o.net_total)}</b></div>
					${o.delivery_status ? `<div class="text-muted">${__(o.delivery_status, null, "Resto")}</div>` : ""}
					${o.ready_count ? `<span class="rf-ready">${__("Ready")} ${o.ready_count}</span>` : ""}
				</div>`
				)
				.join("")}</div>`
		);
		this.$body.find(".rf-ocard").on("click", (e) => this.load_order($(e.currentTarget).attr("data-order")));
	}

	open_table(t) {
		if (t.order) return this.load_order(t.order.name);
		if (t.reservation) {
			frappe.confirm(
				__("Seat the reservation of {0} on this table?", [esc(t.reservation.customer_name)]),
				async () => {
					const name = await call("seat_reservation", { reservation: t.reservation.name, table: t.name }, true);
					this.load_order(name);
				},
				() => this.ask_guests(t)
			);
			return;
		}
		this.ask_guests(t);
	}

	ask_guests(t) {
		const d = new frappe.ui.Dialog({
			title: __("Open Table {0}", [t.name]),
			fields: [{ fieldname: "guests", fieldtype: "Int", label: __("Guests"), default: 2, reqd: 1 }],
			primary_action_label: __("Open", null, "Resto"),
			primary_action: async (v) => {
				d.hide();
				const name = await call("create_order", { order_type: "Dine In", table: t.name, guests: v.guests }, true);
				this.load_order(name);
			},
		});
		d.show();
	}

	// ------------------------------------------------------------------ customer

	// Phone first: what is typed in the phone or the name finds customers; one is picked with a click
	// (its phone, and on a delivery its address, fill in). Editing the phone or the name after picking
	// lets the customer go. The cashier can save a new customer once a phone is typed.
	customer_fields(o, with_address) {
		return [
			{ fieldname: "phone", fieldtype: "Data", label: __("Phone"), default: o.phone || "" },
			{ fieldname: "customer_name", fieldtype: "Data", label: __("Customer Name"), default: o.customer_name || "" },
			{ fieldname: "matches", fieldtype: "HTML" },
			{ fieldname: "address", fieldtype: "Small Text", label: __("Address"), default: o.address || "", hidden: with_address ? 0 : 1 },
			{ fieldname: "new_customer", fieldtype: "Check", label: __("Save as a new customer"), default: 0, hidden: 1 },
		];
	}

	bind_customer(d, o, tick_new) {
		const cashier = !!(this.floor && this.floor.roles.cashier);
		const $m = d.fields_dict.matches.$wrapper;
		const st = { picked: o.customer ? { name: o.customer, customer_name: o.customer_name, mobile_no: o.phone } : null, filled: {}, seq: 0 };
		d.resto_customer = st;
		let found = [];
		const digits = (v) => (v || "").replace(/[^0-9٠-٩۰-۹]/g, "");
		const show = () => {
			const p = st.picked;
			// A new customer is offered to the cashier once a phone is typed and no one is picked.
			const offer = cashier && !p && digits(d.get_value("phone")).length >= 7;
			d.set_df_property("new_customer", "hidden", offer ? 0 : 1);
			if (offer && tick_new && !st.offered) {
				st.offered = true;
				d.set_value("new_customer", 1);
			}
			if (p) {
				$m.html(`<div class="rf-cust-picked">✓ ${esc(p.customer_name || p.name)} ${esc(p.mobile_no || "")}
					<button class="btn btn-xs btn-default" data-unpick>${__("Change Customer")}</button></div>`);
				$m.find("[data-unpick]").on("click", () => unpick(true));
				return;
			}
			$m.html(
				found
					.map(
						(c, i) => `<button class="btn btn-default btn-sm rf-cust" data-i="${i}">
							<b>${esc(c.customer_name)}</b> ${esc(c.mobile_no || "")}
							${c.resto_address ? `<span class="text-muted">· ${esc(c.resto_address)}</span>` : ""}</button>`
					)
					.join("")
			);
			$m.find("[data-i]").on("click", (e) => pick(found[+$(e.currentTarget).attr("data-i")]));
		};
		const pick = async (c) => {
			st.picked = c;
			st.filled = {};
			found = [];
			const fill = async (field, value) => {
				if (!value) return;
				st.filled[field] = value;
				await d.set_value(field, value);
			};
			await fill("customer_name", c.customer_name);
			await fill("phone", c.mobile_no);
			if (!d.fields_dict.address.df.hidden && !d.get_value("address")) await fill("address", c.resto_address);
			await d.set_value("new_customer", 0);
			show();
		};
		// clear: what the picked customer filled in goes too (the button), or only the link (typing).
		const unpick = async (clear) => {
			st.picked = null;
			if (clear) {
				for (const [field, value] of Object.entries(st.filled)) {
					if (d.get_value(field) === value) await d.set_value(field, "");
				}
			}
			st.filled = {};
			show();
		};
		let t;
		const search = (txt) => {
			clearTimeout(t);
			const seq = ++st.seq;
			txt = (txt || "").trim();
			if (st.picked || txt.length < 2) {
				found = [];
				return show();
			}
			t = setTimeout(async () => {
				const rows = (await call("search_customers", { txt })) || [];
				if (seq !== st.seq) return; // an older answer, after a newer search
				found = rows;
				show();
			}, 300);
		};
		["phone", "customer_name"].forEach((f) =>
			d.fields_dict[f].$input.on("input", (e) => {
				const v = $(e.currentTarget).val();
				const p = st.picked;
				if (p && v !== (f === "phone" ? p.mobile_no : p.customer_name)) unpick(false);
				search(v);
			})
		);
		show();
	}

	customer_values(d, v) {
		// The picked customer, or none; a new one needs a name and a phone (a name alone stays on the order).
		const out = { ...v };
		delete out.matches;
		out.customer = d.resto_customer.picked ? d.resto_customer.picked.name : "";
		out.new_customer = !out.customer && out.new_customer && (out.customer_name || "").trim() && (out.phone || "").trim() ? 1 : 0;
		return out;
	}

	new_order(order_type) {
		const d = new frappe.ui.Dialog({
			title: __("New {0} Order", [__(order_type, null, "Resto")]),
			fields: this.customer_fields({}, order_type === "Delivery"),
			primary_action_label: __("Start", null, "Resto"),
			primary_action: async (v) => {
				// Hidden once it worked: what was typed stays if the server refuses.
				const name = await call("create_order", { order_type, ...this.customer_values(d, v) }, true);
				d.hide();
				this.load_order(name);
			},
		});
		d.show();
		this.bind_customer(d, {}, true);
		d.fields_dict.phone.$input.focus();
	}

	// ------------------------------------------------------------------ shift (Resto Shift, D-203)

	shift_bar() {
		const s = this.floor.shift;
		if (!s) return "";
		if (s.shift && s.other_profile) {
			// Bills here need a shift on the restaurant's profile: this one is closed first.
			return `<div class="rf-shift rf-shift-none">${__("Your open shift is on POS Profile {0}; close it and open one on {1}", [esc(s.other_profile), esc(s.pos_profile)])}
				<button class="btn btn-xs btn-default" data-shift="close">${__("Close Shift")}</button></div>`;
		}
		if (s.shift) {
			return `<div class="rf-shift rf-shift-open">✓ ${__("Shift open since {0}", [esc(frappe.datetime.str_to_user(s.since))])}
				<button class="btn btn-xs btn-default" data-shift="expense">${__("Drawer Expense")}</button>
				<button class="btn btn-xs btn-default" data-shift="close">${__("Close Shift")}</button></div>`;
		}
		return `<div class="rf-shift rf-shift-none">${s.required ? __("No shift open: bills cannot be settled") : __("No shift open")}
			<button class="btn btn-xs btn-primary" data-shift="open">${__("Open Shift")}</button></div>`;
	}

	async open_shift() {
		const data = await frappe.xcall("resto.resto.shift.opening_data");
		if (data.open_shift) {
			frappe.msgprint(__("You already have an open shift: {0}", [esc(data.open_shift)]));
			return this.refresh();
		}
		// The profile, the cashier and now, then the opening balance of each method.
		const ro = (fieldname, label, value, fieldtype = "Data") => ({ fieldname, label, fieldtype, default: value, read_only: 1 });
		const d = new frappe.ui.Dialog({
			title: __("Open Shift"),
			fields: [
				ro("pos_profile", __("POS Profile"), data.pos_profile),
				ro("company", __("Company"), data.company),
				ro("warehouse", __("Warehouse"), data.warehouse),
				{ fieldtype: "Column Break" },
				ro("cashier", __("Cashier"), data.cashier),
				ro("now", __("Date and Time"), data.now, "Datetime"),
				ro("currency", __("Currency"), data.currency),
				{ fieldtype: "Section Break", label: __("Opening Balance (Optional)") },
				{ fieldtype: "HTML", options: `<p class="text-muted">${__("Count the money in the drawer.")}</p>` },
				...(data.modes.length
					? data.modes.map((m, i) => ({ fieldname: "m_" + i, fieldtype: "Currency", label: m, default: 0, options: "currency" }))
					: [{ fieldtype: "HTML", options: `<p class="text-danger">${__("No payment methods configured for this POS Profile")}</p>` }]),
			],
			primary_action_label: __("Open Shift"),
			primary_action: (v) =>
				this.shift_busy(d, async () => {
					const amounts = {};
					data.modes.forEach((m, i) => (amounts[m] = flt(v["m_" + i])));
					await frappe.xcall("resto.resto.shift.open_shift", { amounts: JSON.stringify(amounts) });
					d.hide();
					frappe.show_alert({ message: __("Shift opened"), indicator: "green" });
					this.refresh();
				}),
		});
		d.show();
	}

	// One tap at a time: the button waits for the server, and the dialog (with what was typed) stays
	// open when the server refuses.
	async shift_busy(d, fn) {
		const $b = d.get_primary_btn();
		if ($b.prop("disabled")) return;
		$b.prop("disabled", true);
		frappe.dom.freeze(__("Please wait…"));
		try {
			await fn();
		} finally {
			frappe.dom.unfreeze();
			$b.prop("disabled", false);
		}
	}

	// The shift's figures: sales, returns, expenses, collected and
	// on account, taxes, the invoices, and per payment method opening, expected, counted and difference.
	// entry: the counted amounts are inputs (data-m); otherwise the closed shift's report.
	shift_report(r, entry) {
		const s = r.summary || {};
		const diff = (v) => {
			const n = flt(v);
			if (Math.abs(n) < 0.005) return `<span class="text-success">✓ ${__("Balanced")}</span>`;
			return `<span class="${n < 0 ? "text-danger" : "text-warning"}">${n < 0 ? __("Short {0}", [money(-n)]) : __("Over {0}", [money(n)])}</span>`;
		};
		const metric = (label, value, note) =>
			`<div class="rf-metric"><div class="text-muted">${label}</div><b>${value}</b>${note ? `<div class="text-muted small">${note}</div>` : ""}</div>`;
		const parts = [];
		// The header: the profile, when the shift started (and ended), and how long it ran.
		const start = r.period_start;
		const end = r.period_end || frappe.datetime.now_datetime();
		const mins = start ? Math.max(0, Math.round(moment(end).diff(moment(start), "minutes"))) : 0;
		parts.push(`<p class="text-muted">${r.pos_profile ? `${__("POS Profile")}: <b>${esc(r.pos_profile)}</b> · ` : ""}
			${start ? `${__("Shift start")}: ${esc(frappe.datetime.str_to_user(start))}` : ""}
			${r.period_end ? ` · ${__("Shift end")}: ${esc(frappe.datetime.str_to_user(r.period_end))}` : ""}
			${start ? ` · ${__("Duration")}: ${__("{0} h {1} min", [Math.floor(mins / 60), mins % 60])}` : ""}</p>`);
		if (entry && r.open_orders) {
			parts.push(`<div class="alert alert-warning">${__("{0} orders are still open: their bills are not in this shift.", [r.open_orders])}</div>`);
		}
		if (r.summary) {
			parts.push(`<div class="rf-metrics">
				${metric(__("Gross Sales"), money(s.gross_sales), __("{0} invoices", [s.sales_count || (r.invoices || []).filter((i) => !i.is_return).length]))}
				${s.returns_count ? metric(__("Returns"), "-" + money(s.returns_total), __("{0} returns", [s.returns_count])) : ""}
				${s.expenses_count ? metric(__("POS Expenses"), "-" + money(s.expenses_total), __("{0} expenses", [s.expenses_count])) : ""}
				${metric(__("Net Sales"), money(s.net_sales), __("After returns"))}
				${metric(__("Collected"), money(s.collected), __("Money taken"))}
				${s.outstanding > 0.005 ? metric(__("On Account"), money(s.outstanding), __("Not collected")) : ""}
				${s.debts_count ? metric(__("Debts Collected"), money(s.debts_total), __("{0} payments", [s.debts_count])) : ""}
				${s.expenses_count ? metric(__("Net Cash Impact"), money(s.net_cash), __("Collected - Expenses")) : ""}
				${metric(__("Tax Collected"), money(s.tax_total))}
			</div>`);
			const inv = (r.invoices || [])
				.map(
					(i) => `<tr><td>${esc(frappe.datetime.str_to_user(i.posting_date))}</td><td>${esc(i.name)}</td><td>${esc(i.customer || "")}</td>
						<td>${i.is_return ? __("Return") : __("Sale")}</td><td class="rf-amt">${money(i.grand_total)}</td>
						<td class="rf-amt">${i.outstanding_amount > 0.005 ? __("Unpaid: {0}", [money(i.outstanding_amount)]) : ""}</td></tr>`
				)
				.join("");
			parts.push(
				inv
					? `<details><summary><b>${__("Invoice Details")}</b> (${(r.invoices || []).length})</summary>
						<table class="table table-condensed"><tr><th>${__("Posting Date")}</th><th>${__("Invoice")}</th><th>${__("Customer")}</th><th>${__("Type")}</th><th>${__("Amount")}</th><th></th></tr>${inv}</table></details>`
					: `<p class="text-muted">${__("No Sales During This Shift")}</p>`
			);
			if ((r.expenses || []).length) {
				parts.push(`<details><summary><b>${__("POS Expenses")}</b></summary><table class="table table-condensed">
					${r.expenses.map((e) => `<tr><td>${esc(e.expense_account)}</td><td>${esc(e.remarks || "")}</td><td class="rf-amt">${money(e.amount)}</td></tr>`).join("")}</table></details>`);
			}
			if ((r.taxes || []).length) {
				parts.push(`<details><summary><b>${__("Tax Summary")}</b></summary><table class="table table-condensed">
					${r.taxes.map((t) => `<tr><td>${esc(t.account_head)}</td><td>${flt(t.rate)}%</td><td class="rf-amt">${money(t.amount)}</td></tr>`).join("")}</table></details>`);
			}
		}
		const blind = entry && !r.summary;
		const list = r.rows || [];
		const rows = list
			.map((p, i) => {
				const counted = entry
					? `<input type="number" step="0.01" min="0" class="form-control input-sm rf-amt" data-m="${i}" value="">`
					: money(p.closing_amount);
				return `<tr><td>${esc(p.mode_of_payment)}</td>
					${blind ? "" : `<td class="rf-amt">${money(p.opening_amount)}</td><td class="rf-amt">${money(p.expected_amount)}</td>`}
					<td class="rf-amt">${counted}</td>
					${blind ? "" : `<td class="rf-amt" data-d="${i}">${entry ? "" : diff(p.difference)}</td>`}</tr>`;
			})
			.join("");
		// The totals of all methods (none while counting blind).
		const sum = (k) => list.reduce((a, p) => a + flt(p[k]), 0);
		const total = blind
			? ""
			: `<tr class="rf-total"><th>${__("Total")}</th><th class="rf-amt">${money(sum("opening_amount"))}</th>
				<th class="rf-amt">${money(sum("expected_amount"))}</th>
				<th class="rf-amt" data-tc>${entry ? "" : money(sum("closing_amount"))}</th>
				<th class="rf-amt" data-td>${entry ? "" : diff(sum("difference"))}</th></tr>`;
		parts.push(`<h5>${__("Payment Reconciliation")}</h5>
			${entry ? `<p class="text-muted">${__("Count the money in the drawer and enter what you counted.")}</p>` : ""}
			<table class="table table-condensed"><tr><th></th>
				${blind ? "" : `<th>${__("Opening")}</th><th>${__("Expected")}</th>`}<th>${__("Counted")}</th>${blind ? "" : `<th>${__("Difference")}</th>`}</tr>
			${rows}${total}</table>`);
		return parts.join("");
	}

	// Money paid out of the drawer: a Journal Entry from the payment method's account; the shift's
	// expected amount goes down by it.
	async drawer_expense() {
		const data = await frappe.xcall("resto.resto.shift.expense_accounts");
		if (!data.modes.length) return frappe.msgprint(__("This shift has no cash payment method"));
		// One id per dialog: a retried click after a lost answer gives the same entry.
		const request_id = frappe.utils.get_random(20);
		const d = new frappe.ui.Dialog({
			title: __("Drawer Expense"),
			fields: [
				{ fieldname: "mode_of_payment", fieldtype: "Select", label: __("Paid From"), options: data.modes.join("\n"), default: data.modes[0], reqd: 1 },
				{
					fieldname: "expense_account",
					fieldtype: "Select",
					label: __("Expense Account"),
					options: [{ value: "", label: "" }, ...data.accounts.map((a) => ({ value: a.name, label: a.account_name }))],
					reqd: 1,
				},
				{ fieldname: "amount", fieldtype: "Currency", label: __("Amount"), reqd: 1 },
				{ fieldname: "remarks", fieldtype: "Data", label: __("For What"), reqd: 1 },
			],
			primary_action_label: __("Save"),
			primary_action: (v) =>
				this.shift_busy(d, async () => {
					const je = await frappe.xcall("resto.resto.shift.add_expense", { ...v, request_id });
					d.hide();
					frappe.show_alert({ message: __("Expense recorded: {0}", [je]), indicator: "green" });
				}),
		});
		d.show();
	}

	async close_shift() {
		const data = await frappe.xcall("resto.resto.shift.closing_data");
		const d = new frappe.ui.Dialog({ title: __("Close Shift"), size: "large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		const $w = d.fields_dict.html.$wrapper;
		$w.html(this.shift_report(data, true));
		const diff_html = (n) =>
			Math.abs(n) < 0.005
				? `<span class="text-success">✓ ${__("Balanced")}</span>`
				: `<span class="${n < 0 ? "text-danger" : "text-warning"}">${n < 0 ? __("Short {0}", [money(-n)]) : __("Over {0}", [money(n)])}</span>`;
		const counted = () => data.rows.map((r, i) => $w.find(`[data-m="${i}"]`).val());
		// The difference as the cashier types, unless the count is blind (Restaurant Settings).
		$w.find("[data-m]").on("input", () => {
			if (data.blind) return;
			const vals = counted();
			data.rows.forEach((row, i) => {
				$w.find(`[data-d="${i}"]`).html(vals[i] === "" ? "" : diff_html(flt(vals[i]) - flt(row.expected_amount)));
			});
			const all = vals.every((v) => v !== "");
			const tc = vals.reduce((a, v) => a + flt(v), 0);
			const te = data.rows.reduce((a, r) => a + flt(r.expected_amount), 0);
			$w.find("[data-tc]").html(all ? money(tc) : "");
			$w.find("[data-td]").html(all ? diff_html(tc - te) : "");
		});
		d.set_primary_action(__("Close Shift"), () => {
			const vals = counted();
			if (vals.some((v) => v === "")) return frappe.msgprint(__("Please enter all closing amounts"));
			const amounts = {};
			data.rows.forEach((r, i) => (amounts[r.mode_of_payment] = flt(vals[i])));
			// Asked once more: a closed shift cannot be opened again.
			const notes = [];
			if (!data.blind) {
				const short = data.rows.reduce((a, r, i) => a + Math.min(0, flt(vals[i]) - flt(r.expected_amount)), 0);
				if (short < -0.005) notes.push(`<p class="text-danger">${__("The drawer is short by {0}.", [money(-short)])}</p>`);
			}
			if (data.open_orders) notes.push(`<p class="text-warning">${__("{0} orders are still open: their bills are not in this shift.", [data.open_orders])}</p>`);
			frappe.confirm(`${notes.join("")}<p>${__("Close the shift? A closed shift cannot be opened again.")}</p>`, () =>
				this.shift_busy(d, async () => {
					const res = await frappe.xcall("resto.resto.shift.close_shift", { amounts: JSON.stringify(amounts) });
					d.hide();
					const done = new frappe.ui.Dialog({ title: __("Shift closed: {0}", [res.closing]), size: "large", fields: [{ fieldname: "html", fieldtype: "HTML" }] });
					done.fields_dict.html.$wrapper.html(this.shift_report(res, false));
					done.show();
					this.refresh();
				})
			);
		});
		d.show();
	}

	// ------------------------------------------------------------------ order panel

	async load_order(name) {
		const order = await call("get_order", { order: name });
		if (!["Open", "Bill Requested"].includes(order.status)) {
			this.close_order();
			frappe.show_alert({ message: __("Order {0} is {1}", [name, __(order.status, null, "Resto")]), indicator: "blue" });
			return;
		}
		this.order = order;
		if (!this.menu) this.menu = await call("get_menu");
		this.render_order();
	}

	async reload_lines(name) {
		// Periodic refresh: update the order's lines and totals but keep the menu, its search and
		// any open dialog as they are.
		const order = await call("get_order", { order: name });
		if (!this.order || this.order.name !== name) return;
		if (!["Open", "Bill Requested"].includes(order.status)) return this.load_order(name);
		const status_changed = order.status !== this.order.status;
		this.order = order;
		if (status_changed) return this.render_order();
		this.render_lines();
	}

	close_order() {
		this.order = null;
		this.$order.hide().empty();
		$(this.page.body).find(".resto-floor").show();
		this.refresh();
	}

	render_order() {
		const o = this.order;
		$(this.page.body).find(".resto-floor").hide();
		const head =
			o.order_type === "Dine In"
				? `${__("Table {0}", [esc(o.resto_table)], "Resto")} · ${__("{0} guests", [o.guests])}`
				: `${__(o.order_type, null, "Resto")} · ${esc(o.customer_name || "")} ${esc(o.phone || "")}`;
		this.$order.show().html(`
			<div class="rf-ohead">
				<button class="btn btn-default btn-sm rf-back">${frappe.utils.is_rtl() ? "→" : "←"} ${__("Floor", null, "Resto")}</button>
				<div class="rf-otitle">${head} <span class="text-muted">${esc(o.name)}</span>
					${o.status === "Bill Requested" ? `<span class="indicator-pill orange">${__("Bill Requested")}</span>` : ""}</div>
				<div class="rf-oactions">
					<button class="btn btn-default btn-sm" data-act="more">${__("More")} ▾</button>
				</div>
			</div>
			<div class="rf-ocols">
				<div class="rf-menu">
					<input class="form-control rf-search" placeholder="${__("Search menu")}">
					<div class="rf-groups"></div>
					<div class="rf-items"></div>
				</div>
				<div class="rf-cart">
					<div class="rf-lines"></div>
					<div class="rf-totals"></div>
					<div class="rf-buttons">
						<button class="btn btn-primary" data-act="send">${__("Send to Kitchen")}</button>
						<button class="btn btn-default" data-act="fire">${__("Fire Course")}</button>
						<button class="btn btn-default" data-act="served">${__("Served")}</button>
						<button class="btn btn-warning" data-act="bill">${__("Print Bill")}</button>
						${this.floor.roles.cashier || this.floor.roles.manager ? `<button class="btn btn-success" data-act="pay">${__("Pay", null, "Resto")}</button>` : ""}
					</div>
				</div>
			</div>`);
		this.$order.find(".rf-back").on("click", () => this.close_order());
		this.$order.find("[data-act]").on("click", (e) => this.action($(e.currentTarget).attr("data-act"), e));
		this.render_menu();
		this.render_lines();
	}

	render_menu(group, search) {
		const $groups = this.$order.find(".rf-groups");
		this._group = group === undefined ? this._group || null : group;
		$groups.html(
			[`<button class="btn btn-xs ${!this._group ? "btn-primary" : "btn-default"}" data-g="">${__("All")}</button>`]
				.concat(
					this.menu.groups.map(
						(g) =>
							`<button class="btn btn-xs ${this._group === g ? "btn-primary" : "btn-default"}" data-g="${esc(g)}">${esc(__(g))}</button>`
					)
				)
				.join("")
		);
		$groups.find("button").on("click", (e) => this.render_menu($(e.currentTarget).attr("data-g") || null));
		const $search = this.$order.find(".rf-search");
		$search.off("input").on("input", frappe.utils.debounce(() => this.render_menu(undefined), 200));
		const txt = ($search.val() || "").toLowerCase();
		const items = this.menu.items.filter(
			(i) =>
				(!this._group || i.item_group === this._group) &&
				(!txt || i.item_name.toLowerCase().includes(txt) || i.item_code.toLowerCase().includes(txt))
		);
		this.$order.find(".rf-items").html(
			items
				.map(
					(i) => `<div class="rf-item" data-item="${esc(i.item_code)}">
					${i.image ? `<img src="${esc(i.image)}" loading="lazy">` : ""}
					<div class="rf-iname">${esc(i.item_name)}</div>
					<div class="rf-irate">${money(i.rate)}${i.has_modifiers ? " ✱" : ""}</div></div>`
				)
				.join("") || `<p class="text-muted">${__("No items. Check prices in the price list {0}.", [esc(this.menu.price_list)])}</p>`
		);
		this.$order.find(".rf-item").on("click", (e) => {
			const item = this.menu.items.find((i) => i.item_code === $(e.currentTarget).attr("data-item"));
			this.add_item(item);
		});
	}

	async add_item(item) {
		if (!item.has_modifiers) {
			await call("add_items", { order: this.order.name, items: [{ item_code: item.item_code, qty: 1 }] });
			frappe.utils.play_sound("click");
			return this.load_order(this.order.name);
		}
		const groups = await call("get_modifiers", { item_code: item.item_code });
		const fields = [{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), default: 1, reqd: 1 }];
		groups.forEach((g, gi) => {
			fields.push({
				fieldtype: "Section Break",
				label:
					esc(g.name) +
					(g.min_select ? ` (${__("required")})` : "") +
					(g.max_select ? ` · ${__("max {0}", [g.max_select])}` : ""),
			});
			g.options.forEach((opt, oi) => {
				fields.push({
					fieldname: `m_${gi}_${oi}`,
					fieldtype: "Check",
					label: esc(opt.option_name) + (opt.price ? ` (+${money(opt.price)})` : ""),
					default: opt.is_default,
				});
			});
		});
		fields.push(
			{ fieldtype: "Section Break" },
			{ fieldname: "notes", fieldtype: "Small Text", label: __("Kitchen Note") },
			{ fieldname: "course", fieldtype: "Select", options: COURSES.map((c) => ({ value: c, label: __(c) })), label: __("Course", null, "Resto") },
			{ fieldname: "hold", fieldtype: "Check", label: __("Hold Until Fired") }
		);
		const d = new frappe.ui.Dialog({
			title: esc(item.item_name),
			fields,
			primary_action_label: __("Add", null, "Resto"),
			primary_action: async (v) => {
				const modifiers = [];
				groups.forEach((g, gi) =>
					g.options.forEach((opt, oi) => {
						if (v[`m_${gi}_${oi}`]) modifiers.push({ group: g.name, option: opt.option_name });
					})
				);
				await call("add_items", {
					order: this.order.name,
					items: [{ item_code: item.item_code, qty: v.qty, notes: v.notes, course: v.course, hold: v.hold, modifiers }],
				});
				d.hide();
				this.load_order(this.order.name);
			},
		});
		d.show();
	}

	render_lines() {
		const o = this.order;
		const lines = o.items.map((d) => {
			const sent = d.kitchen_status !== "Not Sent";
			const voided = d.kitchen_status === "Void";
			return `<div class="rf-line ${voided ? "void" : ""}" data-line="${esc(d.name)}">
				<div class="rf-lmain">
					<b>${esc(d.item_name)}</b>
					${d.modifiers_text ? `<div class="rf-mod">${esc(d.modifiers_text)}</div>` : ""}
					${d.sides_text ? `<div class="rf-mod"><b>${esc(d.sides_text)}</b></div>` : ""}
					${d.extras_text ? `<div class="rf-mod">${esc(d.extras_text)}</div>` : ""}
					${d.notes ? `<div class="rf-mod">✎ ${esc(d.notes)}</div>` : ""}
					<div class="rf-lmeta">
						<span class="rf-status s-${(d.kitchen_status || "").replace(/ /g, "")}">${__(d.kitchen_status, null, "Resto")}</span>
						${d.course ? `<span>${__(d.course)}</span>` : ""}
						${d.hold && !sent ? `<span class="rf-hold">${__("Held")}</span>` : ""}
						${d.billed_qty ? `<span>${__("Billed {0}", [d.billed_qty])}</span>` : ""}
					</div>
				</div>
				<div class="rf-lqty">
					${sent ? `<span>× ${d.qty}</span>` : `<button class="btn btn-xs btn-default" data-q="-1">−</button><span>${d.qty}</span><button class="btn btn-xs btn-default" data-q="1">+</button>`}
				</div>
				<div class="rf-lamt">${money(d.amount)}</div>
				<div class="rf-lmenu"><button class="btn btn-xs btn-default" data-lm="1">⋮</button></div>
			</div>`;
		});
		this.$order.find(".rf-lines").html(lines.join("") || `<p class="text-muted">${__("Tap menu items to add them.")}</p>`);
		this.$order.find("[data-q]").on("click", (e) => {
			const $l = $(e.currentTarget).closest(".rf-line");
			const line = o.items.find((d) => d.name === $l.attr("data-line"));
			const qty = line.qty + parseInt($(e.currentTarget).attr("data-q"));
			if (qty <= 0) return this.line_call("remove_line", { line: line.name });
			this.line_call("update_line", { line: line.name, qty });
		});
		this.$order.find("[data-lm]").on("click", (e) => {
			const line = o.items.find((d) => d.name === $(e.currentTarget).closest(".rf-line").attr("data-line"));
			this.line_menu(line);
		});

		const disc = o.discount_amount ? `<div>${__("Discount")} <span>−${money(o.discount_amount)}</span></div>` : "";
		this.$order.find(".rf-totals").html(`
			<div>${__("Items Total")} <span>${money(o.net_total)}</span></div>${disc}
			${o.service_charge_percent ? `<div class="text-muted">${__("Service Charge")} ${o.service_charge_percent}%</div>` : ""}
			${o.bill_grand_total ? `<div class="rf-grand">${__("Bill Total")} <span>${money(o.bill_grand_total)}</span></div>` : ""}
			${o.invoiced_total ? `<div class="text-muted">${__("Paid So Far")} <span>${money(o.invoiced_total)}</span></div>` : ""}`);
	}

	async line_call(method, args) {
		await call(method, { order: this.order.name, ...args });
		this.load_order(this.order.name);
	}

	line_menu(line) {
		const sent = line.kitchen_status !== "Not Sent";
		if (line.kitchen_status === "Void") return;
		if (sent && !this.floor.roles.manager) {
			frappe.msgprint(__("Only a manager can void an item sent to the kitchen"));
			return;
		}
		const fields = [];
		if (!sent) {
			fields.push(
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Kitchen Note"), default: line.notes },
				{ fieldname: "course", fieldtype: "Select", options: COURSES.map((c) => ({ value: c, label: __(c) })), label: __("Course", null, "Resto"), default: line.course || "" },
				{ fieldname: "hold", fieldtype: "Check", label: __("Hold Until Fired"), default: line.hold },
				{ fieldname: "sides_btn", fieldtype: "HTML" }
			);
		} else {
			fields.push(
				{ fieldname: "reason", fieldtype: "Data", label: __("Void Reason"), reqd: 1 },
				{
					fieldname: "cooked",
					fieldtype: "Check",
					label: __("Already Cooked (post its ingredients as waste)"),
					default: ["In Progress", "Ready", "Served"].includes(line.kitchen_status) ? 1 : 0,
				}
			);
		}
		const d = new frappe.ui.Dialog({
			title: esc(line.item_name),
			fields,
			primary_action_label: sent ? __("Void Item") : __("Save"),
			primary_action: async (v) => {
				d.hide();
				if (sent) await this.line_call("void_line", { line: line.name, reason: v.reason, cooked: v.cooked ? 1 : 0 });
				else await this.line_call("update_line", { line: line.name, notes: v.notes || "", course: v.course || "", hold: v.hold ? 1 : 0 });
			},
		});
		if (!sent && this.order.sides_enabled && line.has_sides) {
			// Sides can be changed (Restaurant Settings) and this dish has some. What was typed in
			// this dialog is saved first, so a kitchen note is not lost.
			d.fields_dict.sides_btn.$wrapper.html(`<button class="btn btn-default btn-sm">${__("Change Sides")}</button>`);
			d.fields_dict.sides_btn.$wrapper.find("button").on("click", async () => {
				const v = d.get_values(true) || {};
				d.hide();
				await call("update_line", { order: this.order.name, line: line.name, notes: v.notes || "", course: v.course || "", hold: v.hold ? 1 : 0 });
				this.sides_dialog([line.name]);
			});
		}
		if (!sent) d.set_secondary_action_label(__("Remove", null, "Resto"));
		if (!sent) d.set_secondary_action(() => { d.hide(); this.line_call("remove_line", { line: line.name }); });
		d.show();
	}

	async action(act, e) {
		const o = this.order;
		if (act === "send") {
			const tickets = await call("send_to_kitchen", { order: o.name }, true);
			frappe.show_alert({ message: tickets.length ? __("Sent to {0} station(s)", [tickets.length]) : __("Nothing new to send"), indicator: tickets.length ? "green" : "blue" });
			return this.load_order(o.name);
		}
		if (act === "fire") {
			const held = [...new Set(o.items.filter((d) => d.hold && d.kitchen_status === "Not Sent").map((d) => d.course || ""))];
			if (!held.length) return frappe.show_alert({ message: __("No held courses"), indicator: "blue" });
			const d = new frappe.ui.Dialog({
				title: __("Fire Course"),
				fields: [{ fieldname: "course", fieldtype: "Select", options: held.map((c) => ({ value: c, label: c ? __(c) : __("No Course") })), default: held[0] }],
				primary_action_label: __("Fire"),
				primary_action: async (v) => { d.hide(); await call("fire_course", { order: o.name, course: v.course }, true); this.load_order(o.name); },
			});
			return d.show();
		}
		if (act === "served") {
			await call("mark_served", { order: o.name });
			return this.load_order(o.name);
		}
		if (act === "bill") {
			if (this._billing) return;
			this._billing = true;
			try {
				const bill = await call("request_bill", { order: o.name }, true);
				if (bill && bill.on_printer) frappe.show_alert({ message: __("Sent to printer"), indicator: "green" });
				else if (bill && bill.print_error) frappe.show_alert({ message: __("Bill requested, but it could not be printed. Tell the manager."), indicator: "red" }, 10);
				else if (bill && bill.pages && bill.pages.length && this.prints_here()) {
					frappe.show_alert({ message: __("Printing the bill…"), indicator: "blue" });
					await resto.print_pages(bill.pages);
				} else if (bill && bill.pages && bill.pages.length) {
					frappe.show_alert({ message: __("Bill requested. Print it from the cashier device (Print Bill there)."), indicator: "orange" }, 8);
				} else frappe.show_alert({ message: __("Bill requested"), indicator: "green" });
			} finally {
				this._billing = false;
			}
			return this.load_order(o.name);
		}
		if (act === "pay") return this.pay();
		if (act === "more") return this.more_menu(e);
	}

	more_menu() {
		const o = this.order;
		const acts = [
			[__("Discount"), () => this.discount()],
			[__("Guests and Customer"), () => this.edit_info()],
		];
		const with_sides = o.sides_enabled ? o.items.filter((d) => d.kitchen_status === "Not Sent" && d.has_sides) : [];
		if (with_sides.length > 1 || (with_sides.length === 1 && flt(with_sides[0].qty) > 1)) {
			acts.push([__("Sides for Several Dishes"), () => this.pick_side_lines(with_sides)]);
		}
		if (o.print_sides || o.items.some((d) => d.sides_text && d.kitchen_status !== "Void")) {
			// One entry that shows its state: ☑ printed, ☐ not printed.
			acts.push([
				`${o.print_sides ? "☑" : "☐"} ${__("Print Sides on the Receipt")}`,
				async () => {
					await call("set_print_sides", { order: o.name, print_sides: o.print_sides ? 0 : 1 }, true);
					frappe.show_alert({ message: o.print_sides ? __("Sides will not be printed on the receipt") : __("Sides will be printed on the receipt"), indicator: "green" });
					this.load_order(o.name);
				},
			]);
		}
		if (o.order_type === "Dine In") {
			acts.push([__("Move to Table"), () => this.move()], [__("Merge Table Into This"), () => this.merge()]);
		}
		if (o.order_type === "Delivery") acts.push([__("Delivery Status"), () => this.delivery()]);
		if (this.floor.roles.manager) acts.push([__("Service Charge"), () => this.service()]);
		acts.push([__("Open Full Form"), () => frappe.set_route("Form", "Resto Order", o.name)], [__("Cancel Order"), () => this.cancel()]);
		const d = new frappe.ui.Dialog({ title: __("Order Actions"), fields: [{ fieldname: "html", fieldtype: "HTML" }] });
		d.fields_dict.html.$wrapper.html(acts.map((a, i) => `<button class="btn btn-default btn-block" data-i="${i}">${a[0]}</button>`).join(""));
		d.fields_dict.html.$wrapper.find("button").on("click", (e) => { d.hide(); acts[$(e.currentTarget).attr("data-i")][1](); });
		d.show();
	}

	pick_side_lines(lines) {
		const d = new frappe.ui.Dialog({
			title: __("Sides for Several Dishes"),
			fields: [
				{
					fieldname: "lines",
					fieldtype: "MultiCheck",
					label: __("Dishes"),
					sort_options: false,
					columns: 1,
					options: lines.map((l) => ({
						label: `${esc(l.item_name)} × ${l.qty}${l.modifiers_text ? " (" + esc(l.modifiers_text) + ")" : ""}`,
						value: l.name,
						checked: 1,
					})),
				},
			],
			primary_action_label: __("Next"),
			primary_action: (v) => {
				if (!v.lines || !v.lines.length) return frappe.show_alert({ message: __("Choose at least one dish"), indicator: "orange" });
				d.hide();
				this.sides_dialog(v.lines, true);
			},
		});
		d.show();
	}

	async sides_dialog(lines, together = false) {
		const o = this.order;
		const r = await call("get_sides", { order: o.name, lines: JSON.stringify(lines), together: together ? 1 : 0 });
		const fields = [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${r.per_dish ? __("Portions for each dish.") : __("Enter the total for the chosen dishes together.")}</p>`,
			},
		];
		r.slots.forEach((s, si) => {
			const free = s.free ? " · " + __("{0} free", [s.free]) : "";
			fields.push({ fieldtype: "Section Break", label: `${esc(s.label)} · ${__("max {0}", [s.max])}${free}${s.shared ? " · " + __("shared", null, "Resto") : ""}` });
			s.options.forEach((opt, oi) => {
				if (s.single && s.options.length === 1) {
					// A side without a group: served or not.
					fields.push({ fieldname: `s_${si}_${oi}`, fieldtype: "Check", label: esc(opt.item_name), default: opt.total > 0 ? 1 : 0 });
				} else {
					// An extra portion's price, when portions above the recipe's are charged.
					const price = opt.price ? ` (+${money(opt.price)} ${__("per extra portion")})` : "";
					fields.push({
						fieldname: `s_${si}_${oi}`,
						fieldtype: "Float",
						label: esc(opt.item_name) + price,
						default: opt.total,
						non_negative: 1,
						onchange: () => show_extras(),
					});
				}
			});
		});
		const priced = r.slots.some((s) => s.free && s.options.some((opt) => opt.price));
		if (priced) fields.push({ fieldtype: "HTML", fieldname: "extras_preview" });
		// What the extra portions will add, worked out as the server does: the recipe's own sides
		// are free first, then the dearest options (sides.py split_extras). For several dishes the
		// set's total is shared out, so this is the amount for all of them together.
		let d = null;
		const show_extras = () => {
			if (!priced || !d) return;
			let total = 0;
			r.slots.forEach((s, si) => {
				if (!s.free) return;
				let left = flt(s.free);
				s.options
					.map((opt, oi) => ({ ...opt, n: flt(d.get_value(`s_${si}_${oi}`)) }))
					.sort((a, b) => (b.default || 0) - (a.default || 0) || flt(b.price) - flt(a.price) || (a.item < b.item ? -1 : a.item > b.item ? 1 : 0))
					.forEach((opt) => {
						const take = Math.min(opt.n, Math.max(left, 0));
						left -= take;
						if (opt.price) total += (opt.n - take) * flt(opt.price);
					});
			});
			if (r.per_dish) total *= flt(r.qty) || 1;
			d.fields_dict.extras_preview.$wrapper.html(
				`<p><b>${__("Extra portions to charge: {0}", [money(total)])}</b>${r.per_dish && flt(r.qty) > 1 ? ` <span class="text-muted">(${__("for {0} dishes", [r.qty])})</span>` : ""}</p>`
			);
		};
		d = new frappe.ui.Dialog({
			title: esc(r.title || __("Change Sides")),
			fields,
			primary_action_label: __("Save"),
			primary_action: async (v) => {
				// Only the slots that changed: the others keep their choice (a shared one too).
				const choice = {};
				r.slots.forEach((s, si) => {
					const items = {};
					let changed = false;
					s.options.forEach((opt, oi) => {
						const n = flt(v[`s_${si}_${oi}`]);
						if (Math.abs(n - flt(opt.total)) > 0.0005) changed = true; // a shown 0.333 is the stored 1/3
						if (n > 0) items[opt.item] = n;
					});
					if (changed) choice[s.key] = items;
				});
				if (!Object.keys(choice).length) return d.hide();
				await call("set_sides", { order: o.name, lines: JSON.stringify(lines), sides_choice: JSON.stringify(choice), together: together ? 1 : 0 }, true);
				d.hide();
				this.load_order(o.name);
			},
		});
		d.show();
		show_extras();
		// One tap selects the number, so typing replaces it.
		d.$wrapper.find("input").on("focus", function () { this.select && this.select(); });
	}

	discount() {
		const o = this.order;
		frappe.prompt(
			[
				{ fieldname: "percentage", fieldtype: "Percent", label: __("Discount %"), default: o.discount_percentage },
				{ fieldname: "amount", fieldtype: "Currency", label: __("Or Discount Amount"), default: o.discount_percentage ? 0 : o.discount_amount },
				{ fieldname: "reason", fieldtype: "Data", label: __("Discount Reason"), default: o.discount_reason },
			],
			async (v) => { await call("apply_discount", { order: o.name, ...v }, true); this.load_order(o.name); },
			__("Discount")
		);
	}

	edit_info() {
		const o = this.order;
		const d = new frappe.ui.Dialog({
			title: __("Guests and Customer"),
			fields: [
				{ fieldname: "guests", fieldtype: "Int", label: __("Guests"), default: o.guests },
				...this.customer_fields(o, o.order_type === "Delivery"),
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), default: o.notes },
			],
			primary_action_label: __("Save"),
			primary_action: async (v) => {
				// No customer picked clears the link (a name typed alone stays on the order).
				await call("update_order_info", { order: o.name, ...this.customer_values(d, v) }, true);
				d.hide();
				this.load_order(o.name);
			},
		});
		d.show();
		this.bind_customer(d, o, false);
	}

	free_tables() {
		return this.floor.halls.flatMap((h) => h.tables.filter((t) => t.status === "free" || t.status === "reserved").map((t) => t.name));
	}

	move() {
		const o = this.order;
		frappe.prompt(
			[{ fieldname: "table", fieldtype: "Select", label: __("Table", null, "Resto"), options: this.free_tables(), reqd: 1 }],
			async (v) => { await call("move_table", { order: o.name, table: v.table }, true); this.load_order(o.name); },
			__("Move to Table")
		);
	}

	merge() {
		const o = this.order;
		const others = this.floor.halls.flatMap((h) => h.tables.filter((t) => t.order && t.order.name !== o.name).map((t) => ({ value: t.order.name, label: `${t.name} · ${t.order.name}` })));
		if (!others.length) return frappe.show_alert({ message: __("No other open tables"), indicator: "blue" });
		frappe.prompt(
			[{ fieldname: "source", fieldtype: "Select", label: __("Table to merge into this one"), options: others, reqd: 1 }],
			async (v) => { await call("merge_orders", { target: o.name, source: v.source }, true); this.load_order(o.name); },
			__("Merge Tables")
		);
	}

	delivery() {
		const o = this.order;
		frappe.prompt(
			[
				{ fieldname: "delivery_status", fieldtype: "Select", label: __("Delivery Status"), options: ["Pending", "Out for Delivery", "Delivered"].map((s) => ({ value: s, label: __(s, null, "Resto") })), default: o.delivery_status || "Pending" },
				{ fieldname: "driver", fieldtype: "Link", options: "Employee", label: __("Driver", null, "Resto"), default: o.driver },
				{ fieldname: "delivery_fee", fieldtype: "Currency", label: __("Delivery Fee"), default: o.delivery_fee },
			],
			async (v) => { await call("set_delivery", { order: o.name, ...v }, true); this.load_order(o.name); },
			__("Delivery", null, "Resto")
		);
	}

	service() {
		const o = this.order;
		frappe.prompt(
			[{ fieldname: "percent", fieldtype: "Percent", label: __("Service Charge %"), default: o.service_charge_percent }],
			async (v) => { await call("set_service_charge", { order: o.name, percent: v.percent }, true); this.load_order(o.name); },
			__("Service Charge")
		);
	}

	cancel() {
		const o = this.order;
		const sent = (o.items || []).filter((d) => !["Not Sent", "Void"].includes(d.kitchen_status));
		if (sent.length && !this.floor.roles.manager) {
			frappe.msgprint(__("Only a manager can cancel an order sent to the kitchen"));
			return;
		}
		const fields = [{ fieldname: "reason", fieldtype: "Small Text", label: __("Cancel Reason"), reqd: 1 }];
		if (sent.length) {
			// What the kitchen already made is posted as waste; what it did not make stays in stock.
			fields.push({
				fieldname: "cooked_help",
				fieldtype: "HTML",
				options: `<p><b>${__("Already cooked?")}</b> <span class="text-muted">${__("Tick the dishes the kitchen already made: their ingredients are posted as waste.")}</span></p>`,
			});
			fields.push(
				...resto.cooked_fields(
					sent,
					(d) => `${esc(d.item_name)} × ${d.qty} (${esc(__(d.kitchen_status, null, "Resto"))})`,
					(d) => ["In Progress", "Ready", "Served"].includes(d.kitchen_status)
				)
			);
		}
		frappe.prompt(
			fields,
			async (v) => {
				const cooked = resto.cooked_values(sent, v, (d) => d.name);
				await call("cancel_order", { order: o.name, reason: v.reason, cooked: JSON.stringify(cooked) }, true);
				this.close_order();
			},
			__("Cancel Order"),
			__("Cancel Order")
		);
	}

	// ------------------------------------------------------------------ payment

	async pay(lines) {
		const o = this.order;
		const bill = await call("get_bill", { order: o.name, lines: lines ? JSON.stringify(lines) : null }, true);
		const modes = bill.payment_modes;
		const fields = [{ fieldname: "summary", fieldtype: "HTML" }];
		modes.forEach((m, i) => {
			fields.push({ fieldname: `p_${i}`, fieldtype: "Currency", label: __(m.mode_of_payment), default: m.default ? bill.payable : 0 });
		});
		fields.push({ fieldname: "quick", fieldtype: "HTML" }, { fieldname: "change", fieldtype: "HTML" });
		// Partial payment (D-202): what is not paid stays owed by the order's named customer, when the
		// POS Profile allows it. Ticked by the cashier, never assumed.
		if (bill.on_account_customer) {
			fields.push({
				fieldname: "on_account",
				fieldtype: "Check",
				label: __("Leave the rest on the account of {0}", [bill.on_account_customer_name || bill.on_account_customer]),
				hidden: 1,
			});
		}
		// One id per dialog: a retried click after a lost answer returns the same invoice.
		const request_id = frappe.utils.get_random(20);
		const d = new frappe.ui.Dialog({
			title: lines ? __("Pay Selected Items") : __("Pay Bill"),
			size: "large",
			fields,
			primary_action_label: __("Settle"),
			primary_action: async (v) => {
				const payments = modes.map((m, i) => ({ mode_of_payment: m.mode_of_payment, amount: v[`p_${i}`] || 0 }));
				d.get_primary_btn().prop("disabled", true);
				try {
					const r = await call(
						"settle",
						{
							order: o.name,
							payments: JSON.stringify(payments),
							lines: lines ? JSON.stringify(lines) : null,
							expected: bill.payable,
							request_id,
							on_account: remaining() > 0.005 && v.on_account ? 1 : 0,
						},
						true
					);
					d.hide();
					this.receipt_mode = r.receipt_mode;
					const print_receipt = () => this.print_receipt(r.sales_invoice, r.receipt_mode);
					// Printed once by itself, on the device that prints receipts: a retried payment (same
					// invoice) does not print it again.
					const auto = r.receipt_mode === "auto" && !r.receipt_printed && this.prints_here();
					frappe.msgprint({
						title: __("Paid"),
						indicator: "green",
						message: `${__("Invoice")} <a href="/app/sales-invoice/${esc(r.sales_invoice)}">${esc(r.sales_invoice)}</a><br>
							${r.change_amount ? `<b>${__("Change", null, "Resto")}: ${money(r.change_amount)}</b>` : ""}
							${r.outstanding_amount > 0.005 ? `<b class="text-warning">${__("On account of {0}: {1}", [esc(bill.on_account_customer_name || bill.on_account_customer), money(r.outstanding_amount)])}</b>` : ""}`,
						primary_action: {
							// The receipt printed by itself (cashier printer, or this browser): this prints it again.
							label: r.receipt_mode === "server" || auto || r.receipt_printed ? __("Print Again") : __("Print Receipt"),
							action: () => print_receipt(),
						},
					});
					if (auto) print_receipt();
					if (r.status === "Paid") this.close_order();
					else this.load_order(o.name);
				} finally {
					d.get_primary_btn().prop("disabled", false);
				}
			},
		});
		if (!lines) {
			d.set_secondary_action_label(__("Split by Items"));
			d.set_secondary_action(() => { d.hide(); this.split(); });
		}
		const taxes = bill.taxes.map((t) => `<tr><td>${esc(t.description)}</td><td class="rf-amt">${money(t.amount)}</td></tr>`).join("");
		d.fields_dict.summary.$wrapper.html(`
			<table class="table table-condensed rf-bill">
				<tr><td>${__("Items Total")}</td><td class="rf-amt">${money(bill.total)}</td></tr>
				${bill.discount ? `<tr><td>${__("Discount")}</td><td class="rf-amt"><span dir="ltr">−${money(bill.discount)}</span></td></tr>` : ""}
				${taxes}
				${bill.rounding_adjustment ? `<tr><td>${__("Rounding")}</td><td class="rf-amt">${money(bill.rounding_adjustment)}</td></tr>` : ""}
				<tr class="rf-grand"><td>${__("To Pay")}</td><td class="rf-amt">${money(bill.payable)}</td></tr>
				${bill.guests > 1 ? `<tr class="text-muted"><td>${__("Per Guest")} (${bill.guests})</td><td class="rf-amt">${money(bill.per_guest)}</td></tr>` : ""}
			</table>`);
		const remaining = () => bill.payable - modes.reduce((s, m, i) => s + (d.get_value(`p_${i}`) || 0), 0);
		const update_change = () => {
			const diff = -remaining();
			let note = "";
			if (diff < -0.005 && !bill.on_account_customer && bill.allow_partial_payment) {
				note = `<p class="text-muted">${__("To leave the rest owed, choose the customer in Guests and Customer first (not the walk-in customer).")}</p>`;
			}
			d.fields_dict.change.$wrapper.html(
				(diff >= -0.005
					? `<h4>${__("Change", null, "Resto")}: ${money(Math.max(diff, 0))}</h4>`
					: `<h4 class="text-danger">${__("Remaining")}: ${money(-diff)}</h4>`) + note
			);
			if (d.fields_dict.on_account) d.set_df_property("on_account", "hidden", diff < -0.005 ? 0 : 1);
		};
		modes.forEach((m, i) => d.fields_dict[`p_${i}`].$input.on("input change", update_change));
		// "All by card" and friends: one tap puts the whole bill on one method and clears the others.
		d.fields_dict.quick.$wrapper.html(
			`<div class="rf-quick">${modes
				.map((m, i) => `<button class="btn btn-default btn-sm" data-i="${i}">${__("All by {0}", [esc(__(m.mode_of_payment))])}</button>`)
				.join("")}</div>`
		);
		d.fields_dict.quick.$wrapper.find("button").on("click", async (e) => {
			const pick = cint($(e.currentTarget).attr("data-i"));
			for (let i = 0; i < modes.length; i++) await d.set_value(`p_${i}`, i === pick ? bill.payable : 0);
			update_change();
		});
		update_change();
		d.show();
	}

	split() {
		const o = this.order;
		const open = o.items.filter((d) => d.kitchen_status !== "Void" && d.qty - (d.billed_qty || 0) > 0);
		const fields = open.map((d, i) => ({
			fieldname: `l_${i}`,
			fieldtype: "Float",
			label: `${esc(d.item_name)}${d.modifiers_text ? " (" + esc(d.modifiers_text) + ")" : ""} · ${money(d.rate + (d.modifiers_amount || 0) + (d.sides_amount || 0))} · ${__("left")} ${d.qty - (d.billed_qty || 0)}`,
			default: 0,
		}));
		const d = new frappe.ui.Dialog({
			title: __("Split by Items"),
			fields,
			primary_action_label: __("Pay These"),
			primary_action: (v) => {
				const lines = {};
				open.forEach((row, i) => { if (v[`l_${i}`] > 0) lines[row.name] = v[`l_${i}`]; });
				if (!Object.keys(lines).length) return frappe.msgprint(__("Enter a quantity for at least one item"));
				d.hide();
				this.pay(lines);
			},
		});
		d.show();
	}

	static css() {
		return `
		.rf-shift { flex-basis:100%; border-radius:8px; padding:6px 10px; margin-bottom:6px; font-weight:600; display:flex; gap:10px; align-items:center; }
		.rf-shift-open { background:var(--green-50, #f0fff4); color:var(--green-800, #276749); border:1px solid var(--green-300, #9ae6b4); }
		.rf-shift-none { background:var(--yellow-50, #fffff0); color:var(--yellow-800, #975a16); border:2px solid var(--yellow-400, #f6e05e); }
		.rf-cust { display:block; width:100%; text-align:start; margin-bottom:4px; white-space:normal; }
		.rf-cust-picked { font-weight:600; color:var(--green-700, #2f855a); margin-bottom:6px; }
		.rf-metrics { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:8px; margin-bottom:10px; }
		.rf-metric { border:1px solid var(--border-color); border-radius:8px; padding:6px 10px; }
		.rf-print-alert { flex-basis:100%; border-radius:8px; padding:6px 10px; margin-bottom:6px; font-weight:600; background:var(--red-100, #fed7d7); color:var(--red-800, #9b2c2c); border:2px solid var(--red-500, #f56565); }
		.resto-floor .rf-tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
		.rf-count { opacity:.7; font-size:11px; margin-inline-start:4px; }
		.rf-grid { display:grid; gap:12px; }
		.rf-table, .rf-ocard { position:relative; border-radius:10px; padding:12px; min-height:110px; cursor:pointer; border:2px solid transparent; background:var(--bg-light-gray); user-select:none; }
		.rf-amt { text-align:end; white-space:nowrap; }
		.rf-quick { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
		.rf-table.round { border-radius:50%; text-align:center; display:flex; flex-direction:column; justify-content:center; aspect-ratio:1; }
		.rf-table.free { background:var(--green-50, #e9f9ee); border-color:var(--green-300, #9ae6b4); }
		.rf-table.occupied, .rf-ocard.occupied { background:var(--blue-50, #ebf8ff); border-color:var(--blue-400, #63b3ed); }
		.rf-table.bill, .rf-ocard.bill { background:var(--orange-50, #fffaf0); border-color:var(--orange-400, #f6ad55); }
		.rf-table.reserved { background:var(--purple-50, #faf5ff); border-color:var(--purple-300, #d6bcfa); }
		.rf-name { font-weight:700; font-size:16px; }
		.rf-amount { font-weight:600; }
		.rf-waiter, .rf-seats { font-size:12px; opacity:.75; }
		.rf-ready { position:absolute; top:6px; inset-inline-end:6px; background:var(--green-600, #38a169); color:#fff; border-radius:10px; padding:0 8px; font-size:11px; }
		.rf-legend { margin-top:14px; font-size:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
		.rf-dot { display:inline-block; width:12px; height:12px; border-radius:3px; margin-inline-end:4px; }
		.rf-dot.free { background:#9ae6b4 } .rf-dot.occupied { background:#63b3ed } .rf-dot.bill { background:#f6ad55 } .rf-dot.reserved { background:#d6bcfa }
		.rf-list { display:grid; grid-template-columns:repeat(auto-fill, minmax(220px,1fr)); gap:12px; }
		.rf-ohead { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }
		.rf-otitle { flex:1; font-weight:700; font-size:16px; }
		.rf-ocols { display:grid; grid-template-columns: 1.4fr 1fr; gap:14px; }
		@media (max-width: 900px) { .rf-ocols { grid-template-columns: 1fr; } }
		.rf-groups { display:flex; flex-wrap:wrap; gap:4px; margin:8px 0; }
		.rf-items { display:grid; grid-template-columns:repeat(auto-fill, minmax(120px,1fr)); gap:8px; max-height:70vh; overflow:auto; }
		.rf-item { border:1px solid var(--border-color); border-radius:8px; padding:8px; cursor:pointer; text-align:center; background:var(--card-bg); }
		.rf-item:active { transform:scale(.97); }
		.rf-item img { width:100%; height:70px; object-fit:cover; border-radius:6px; }
		.rf-iname { font-weight:600; font-size:13px; }
		.rf-irate { font-size:12px; opacity:.8; }
		.rf-cart { border:1px solid var(--border-color); border-radius:10px; padding:10px; display:flex; flex-direction:column; max-height:80vh; }
		.rf-lines { flex:1; overflow:auto; }
		.rf-line { display:grid; grid-template-columns: 1fr auto auto auto; gap:6px; align-items:center; padding:6px 0; border-bottom:1px dashed var(--border-color); }
		.rf-line.void { opacity:.45; text-decoration:line-through; }
		.rf-mod { font-size:12px; opacity:.8; }
		.rf-lmeta { font-size:11px; display:flex; gap:6px; flex-wrap:wrap; }
		.rf-status { padding:0 6px; border-radius:8px; background:var(--gray-100); }
		.rf-status.s-Ready { background:#c6f6d5 } .rf-status.s-InProgress { background:#feebc8 } .rf-status.s-Sent { background:#bee3f8 } .rf-status.s-Void { background:#fed7d7 }
		.rf-hold { color:var(--orange-600, #dd6b20); font-weight:600; }
		.rf-lqty { display:flex; gap:4px; align-items:center; }
		.rf-totals > div { display:flex; justify-content:space-between; padding:2px 0; }
		.rf-grand { font-weight:700; font-size:16px; }
		.rf-buttons { display:grid; grid-template-columns: repeat(2, 1fr); gap:6px; margin-top:8px; }
		.rf-buttons .btn { padding:10px 4px; }
		.rf-bill td { padding:4px 8px !important; }`;
	}
}
