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
		this.$tabs.html(this.kitchen_print_alert() + tabs.join(""));
		this.$tabs.find("button").on("click", (e) => {
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

	new_order(order_type) {
		const d = new frappe.ui.Dialog({
			title: __("New {0} Order", [__(order_type, null, "Resto")]),
			fields: [
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer") },
				{ fieldname: "customer_name", fieldtype: "Data", label: __("Customer Name") },
				{ fieldname: "phone", fieldtype: "Data", label: __("Phone") },
				{
					fieldname: "address",
					fieldtype: "Small Text",
					label: __("Address"),
					depends_on: `eval:${order_type === "Delivery"}`,
				},
			],
			primary_action_label: __("Start", null, "Resto"),
			primary_action: async (v) => {
				d.hide();
				const name = await call("create_order", { order_type, ...v }, true);
				this.load_order(name);
			},
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
		frappe.prompt(
			[
				{ fieldname: "guests", fieldtype: "Int", label: __("Guests"), default: o.guests },
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer"), default: o.customer },
				{ fieldname: "customer_name", fieldtype: "Data", label: __("Customer Name"), default: o.customer_name },
				{ fieldname: "phone", fieldtype: "Data", label: __("Phone"), default: o.phone },
				{ fieldname: "address", fieldtype: "Small Text", label: __("Address"), default: o.address },
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), default: o.notes },
			],
			async (v) => { await call("update_order_info", { order: o.name, ...v }, true); this.load_order(o.name); },
			__("Guests and Customer")
		);
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
		frappe.prompt(
			[{ fieldname: "reason", fieldtype: "Small Text", label: __("Cancel Reason"), reqd: 1 }],
			async (v) => { await call("cancel_order", { order: o.name, reason: v.reason }, true); this.close_order(); },
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
							${r.change_amount ? `<b>${__("Change", null, "Resto")}: ${money(r.change_amount)}</b>` : ""}`,
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
		const update_change = () => {
			const paid = modes.reduce((s, m, i) => s + (d.get_value(`p_${i}`) || 0), 0);
			const diff = paid - bill.payable;
			d.fields_dict.change.$wrapper.html(
				diff >= 0 ? `<h4>${__("Change", null, "Resto")}: ${money(diff)}</h4>` : `<h4 class="text-danger">${__("Remaining")}: ${money(-diff)}</h4>`
			);
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
