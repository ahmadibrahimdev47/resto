// Kitchen display: tickets of one station (or all), oldest first, with timers.

frappe.pages["resto-kitchen"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Kitchen Display"), single_column: true });
	wrapper.resto_kitchen = new RestoKitchen(page, wrapper);
};

frappe.pages["resto-kitchen"].on_page_show = function (wrapper) {
	wrapper.resto_kitchen && wrapper.resto_kitchen.refresh();
};

class RestoKitchen {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.station = localStorage.getItem("resto_kds_station") || "";
		this.known = null;
		this.page.add_inner_button(__("Refresh"), () => this.refresh());
		this.page.add_inner_button(__("Full Screen"), () => document.documentElement.requestFullscreen && document.documentElement.requestFullscreen());
		this.station_field = this.page.add_field({
			fieldname: "station",
			label: __("Station"),
			fieldtype: "Link",
			options: "Resto Kitchen Station",
			default: this.station,
			change: () => {
				this.station = this.station_field.get_value() || "";
				localStorage.setItem("resto_kds_station", this.station);
				this.known = null;
				this.refresh();
			},
		});
		$(this.page.body).html(`<style>${RestoKitchen.css()}</style><div class="rk-board"></div>`);
		this.$board = $(this.page.body).find(".rk-board");
		frappe.realtime.on("resto_update", (d) => {
			if (d && (d.kind === "ticket" || d.kind === "order")) {
				clearTimeout(this._t);
				this._t = setTimeout(() => this.refresh(), 300);
			}
		});
		setInterval(() => this.tick(), 15000);
		// Browsers keep sound off until the screen is touched once; one shared context is resumed then.
		$(document).one("click touchstart keydown", () => this.audio());
		this.poll_seconds = 15;
		this._poll = setInterval(() => this.refresh(), this.poll_seconds * 1000);
		this.refresh();
	}

	refresh() {
		if (!$(this.wrapper).is(":visible")) return;
		frappe.call({
			method: "resto.resto.kitchen.kds_data",
			args: { station: this.station },
			callback: (r) => {
				const data = r.message;
				this.offset = frappe.datetime.str_to_obj(data.now) - new Date();
				const names = new Set(data.tickets.filter((t) => t.status === "New").map((t) => t.name));
				if (this.known && [...names].some((n) => !this.known.has(n))) this.beep();
				this.known = names;
				this.tickets = data.tickets;
				this.render();
				const seconds = data.refresh_seconds || 15;
				if (seconds !== this.poll_seconds) {
					this.poll_seconds = seconds;
					clearInterval(this._poll);
					this._poll = setInterval(() => this.refresh(), seconds * 1000);
				}
			},
		});
	}

	minutes(since) {
		if (!since) return 0;
		const now = new Date(Date.now() + (this.offset || 0));
		return Math.max(0, Math.floor((now - frappe.datetime.str_to_obj(since)) / 60000));
	}

	tick() {
		this.$board.find("[data-since]").each((i, el) => {
			const m = this.minutes($(el).attr("data-since"));
			$(el).text(`${m}′`).closest(".rk-ticket").toggleClass("late", m >= 20).toggleClass("warn", m >= 10 && m < 20);
		});
	}

	render() {
		const esc = frappe.utils.escape_html;
		if (!this.tickets.length) {
			this.$board.html(`<div class="rk-empty">${__("No tickets")}</div>`);
			return;
		}
		const next = { New: "In Progress", "In Progress": "Ready", Ready: "Served" };
		const label = { New: __("Start", null, "Resto"), "In Progress": __("Ready"), Ready: __("Served") };
		this.$board.html(
			this.tickets
				.map((t) => {
					const where = t.table ? __("Table {0}", [esc(t.table)], "Resto") : `${__(t.order_type, null, "Resto")} · ${esc(t.order || "")}`;
					const m = this.minutes(t.sent_at);
					const items = t.items
						.map(
							(d) => `<li class="${d.voided ? "rk-voided" : ""}"><b>${d.qty} × ${esc(d.item_name)}</b>${d.voided ? ` <span class="rk-vtag">${__("VOID")}</span>` : ""}
							${d.course ? `<span class="rk-course">${__(d.course)}</span>` : ""}
							${d.modifiers_text ? `<div class="rk-mod">${esc(d.modifiers_text)}</div>` : ""}
							${d.notes ? `<div class="rk-note">✎ ${esc(d.notes)}</div>` : ""}</li>`
						)
						.join("");
					const isVoid = t.ticket_type === "Void";
					const btn =
						t.status === "Served"
							? `<button class="btn btn-default btn-sm" data-s="Ready">${__("Recall")}</button>`
							: isVoid
								? `<button class="btn btn-danger btn-sm" data-s="Served">${__("Seen", null, "Resto")}</button>`
								: `<button class="btn btn-primary btn-sm" data-s="${next[t.status]}">${label[t.status]}</button>`;
					return `<div class="rk-ticket s-${t.status.replace(/ /g, "")} ${isVoid ? "void" : ""} ${m >= 20 ? "late" : m >= 10 ? "warn" : ""}" data-name="${esc(t.name)}">
						<div class="rk-head"><b>${where}</b><span data-since="${t.sent_at}">${m}′</span></div>
						<div class="rk-sub">${isVoid ? `<b>${__("VOID")}</b> · ` : ""}${esc(t.station || "")} · ${esc(t.waiter || "")} · ${__(t.status, null, "Resto")}</div>
						<ul>${items}</ul>
						<div class="rk-actions">${btn}
							<button class="btn btn-default btn-sm" data-print="1">${__("Print", null, "Resto")}</button></div>
					</div>`;
				})
				.join("")
		);
		this.$board.find("[data-s]").on("click", (e) => {
			const name = $(e.currentTarget).closest(".rk-ticket").attr("data-name");
			frappe.call({
				method: "resto.resto.kitchen.kds_set_status",
				args: { ticket: name, status: $(e.currentTarget).attr("data-s") },
				callback: () => this.refresh(),
			});
		});
		this.$board.find("[data-print]").on("click", (e) => {
			const name = $(e.currentTarget).closest(".rk-ticket").attr("data-name");
			frappe.call({
				method: "resto.resto.kitchen.kds_print",
				args: { ticket: name },
				callback: () => frappe.show_alert({ message: __("Sent to printer"), indicator: "green" }),
				error: () =>
					window.open(frappe.urllib.get_full_url(`/printview?doctype=Resto%20Kitchen%20Ticket&name=${encodeURIComponent(name)}&format=Resto%20Kitchen%20Ticket&no_letterhead=1&trigger_print=1`)),
			});
		});
	}

	audio() {
		try {
			if (!this._audio) this._audio = new (window.AudioContext || window.webkitAudioContext)();
			if (this._audio.state === "suspended") this._audio.resume();
		} catch (e) {
			this._audio = null;
		}
		return this._audio;
	}

	beep() {
		try {
			const ctx = this.audio();
			if (!ctx) return;
			const osc = ctx.createOscillator();
			osc.frequency.value = 880;
			osc.connect(ctx.destination);
			osc.start();
			osc.stop(ctx.currentTime + 0.25);
		} catch (e) {
			// no sound available
		}
	}

	static css() {
		return `
		.rk-board { display:grid; grid-template-columns:repeat(auto-fill, minmax(260px,1fr)); gap:12px; align-items:start; }
		.rk-empty { font-size:20px; opacity:.6; padding:40px; text-align:center; grid-column:1/-1; }
		.rk-ticket { border-radius:10px; border:3px solid var(--blue-400, #63b3ed); background:var(--card-bg); padding:10px; }
		.rk-ticket.s-InProgress { border-color:var(--orange-400, #f6ad55); }
		.rk-ticket.s-Ready { border-color:var(--green-500, #48bb78); opacity:.8; }
		.rk-ticket.s-Served { opacity:.45; }
		.rk-ticket.warn .rk-head span { color:var(--orange-600, #dd6b20); }
		.rk-ticket.late .rk-head span { color:var(--red-600, #e53e3e); font-weight:700; }
		.rk-ticket.void { border-color:var(--red-500, #f56565); background:var(--red-50, #fff5f5); }
		.rk-head { display:flex; justify-content:space-between; font-size:17px; }
		.rk-sub { font-size:12px; opacity:.75; margin-bottom:6px; }
		.rk-ticket ul { padding-inline-start:18px; margin:0 0 8px; font-size:15px; }
		.rk-ticket.void ul, .rk-voided b { text-decoration:line-through; }
		.rk-voided { opacity:.55; }
		.rk-vtag { background:#E24C4C; color:#fff; border-radius:4px; padding:0 4px; font-size:11px; text-decoration:none; }
		.rk-mod { font-size:13px; color:var(--text-muted); }
		.rk-note { font-size:13px; color:var(--red-600, #e53e3e); font-weight:600; }
		.rk-course { font-size:11px; background:var(--gray-100); border-radius:6px; padding:0 6px; margin-inline-start:4px; }
		.rk-actions { display:flex; gap:6px; }
		.rk-actions .btn-primary, .rk-actions .btn-danger { flex:1; padding:8px; font-size:15px; }`;
	}
}
