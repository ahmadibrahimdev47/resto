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
		this.print_methods = {};
		// The stations this screen prints new tickets for by itself (stations set to print
		// automatically from the browser): kept per browser, so only the computer with the kitchen
		// printer turns it on, and apart from the station shown.
		this.print_stations = [];
		try {
			this.print_stations = JSON.parse(localStorage.getItem("resto_kds_print_stations") || "[]") || [];
		} catch (e) {
			this.print_stations = [];
		}
		this.print_alerts = {};
		// Paused when a print window opened (Chrome not started from the printing shortcut).
		this.paused = false;
		// Printing goes on while another page of the system is shown on this computer.
		this._claim_poll = setInterval(() => this.print_stations.length && !this.paused && this.claim_prints(), 10000);
		this.page.add_inner_button(__("Refresh"), () => this.refresh());
		// Only a kitchen user prints automatically (the server takes the tickets for that role only).
		this.can_autoprint = frappe.user.has_role("Resto Kitchen") || frappe.user.has_role("Resto Manager") || frappe.session.user === "Administrator";
		// Waste for a cancelled dish already made, from its void ticket.
		this.can_post_waste = ["Resto Kitchen", "Resto Cashier", "Resto Manager", "System Manager"].some((r) => frappe.user.has_role(r));
		if (!this.can_autoprint) this.print_stations = [];
		if (this.can_autoprint) {
			this.$autoprint = this.page.add_inner_button(__("Auto Printing: Off"), () => this.toggle_autoprint());
			this.show_autoprint();
		}
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
		$(this.page.body).html(`<style>${RestoKitchen.css()}</style><div class="rk-alerts"></div><div class="rk-board"></div>`);
		this.$alerts = $(this.page.body).find(".rk-alerts");
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
				this.print_methods = data.print_methods || {};
				this.print_alerts = data.print_alerts || {};
				this.drop_stations_not_automatic();
				this.render();
				// On every refresh: it prints what is new and tells the other screens this one prints.
				if (this.print_stations.length && !this.paused) this.claim_prints();
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
		this.render_alerts();
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
							${d.sides_text ? `<div class="rk-sides">${esc(d.sides_text)}</div>` : ""}
							${d.notes ? `<div class="rk-note">✎ ${esc(d.notes)}</div>` : ""}</li>`
						)
						.join("");
					const isVoid = t.ticket_type === "Void";
					const method = this.print_methods[t.station];
					const prints = method && method !== "No Printing";
					// Red only where tickets print by themselves; a Print button station often stays on screen.
					const missed = method === "Browser Automatic" || method === "Server Printer" ? "rk-unprinted" : "rk-unprinted-muted";
					const paper = prints
						? `<span class="${t.printed ? "rk-printed" : missed}">${t.printed ? "🖨 " + __("Sent to printer") : "🖨 " + __("Not Printed")}</span>`
						: "";
					const btn =
						t.status === "Served"
							? `<button class="btn btn-default btn-sm" data-s="Ready">${__("Recall")}</button>`
							: isVoid
								? `<button class="btn btn-danger btn-sm" data-s="Served">${__("Seen", null, "Resto")}</button>`
								: `<button class="btn btn-primary btn-sm" data-s="${next[t.status]}">${label[t.status]}</button>`;
					return `<div class="rk-ticket s-${t.status.replace(/ /g, "")} ${isVoid ? "void" : ""} ${m >= 20 ? "late" : m >= 10 ? "warn" : ""}" data-name="${esc(t.name)}">
						<div class="rk-head"><b>${t.daily_no ? `<span class="rk-no">#${t.daily_no}</span> ` : ""}${where}</b><span data-since="${t.sent_at}">${m}′</span></div>
						<div class="rk-sub">${isVoid ? `<b>${__("VOID")}</b> · ` : ""}${esc(t.station || "")} · ${esc(t.waiter || "")} · ${__(t.status, null, "Resto")} ${paper}</div>
						<ul>${items}</ul>
						<div class="rk-actions">${btn}
							${isVoid && this.can_post_waste && (t.items || []).some((d) => d.waste_open) ? `<button class="btn btn-warning btn-sm" data-waste="1">${__("Cooked: Post as Waste")}</button>` : ""}
							<button class="btn btn-default btn-sm" data-print="1">${t.printed ? __("Reprint") : __("Print", null, "Resto")}</button></div>
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
		this.$board.find("[data-waste]").on("click", (e) => {
			const name = $(e.currentTarget).closest(".rk-ticket").attr("data-name");
			const t = (this.tickets || []).find((x) => x.name === name);
			if (t) this.post_waste(t);
		});
		this.$board.find("[data-print]").on("click", async (e) => {
			const btn = $(e.currentTarget);
			if (btn.prop("disabled")) return;
			btn.prop("disabled", true);
			const name = btn.closest(".rk-ticket").attr("data-name");
			try {
				const res = (await frappe.xcall("resto.resto.kitchen.kds_print", { ticket: name })) || {};
				if (res.server) return frappe.show_alert({ message: __("Sent to printer"), indicator: "green" });
				resto.print_window_seen = resto.print_slow = false;
				if (!(await resto.print_pages(res.pages || []))) return;
				const confirm = () => frappe.xcall("resto.resto.kitchen.kds_printed", { ticket: name }).then(() => this.refresh());
				// A print window can be cancelled: only the cook knows whether the paper came out.
				if (resto.print_slow || resto.print_window_seen) frappe.confirm(__("Did the ticket come out of the printer?"), confirm);
				else confirm();
			} finally {
				btn.prop("disabled", false);
			}
		});
	}

	show_autoprint() {
		if (!this.$autoprint) return;
		const on = this.print_stations.length > 0 && !this.paused;
		const names = frappe.utils.escape_html(this.print_stations.join("، "));
		this.$autoprint
			.html(
				this.paused
					? `🖨 ${__("Auto Printing: Paused")} (${names})`
					: on
						? `🖨 ${__("Auto Printing: On")} (${names})`
						: `🖨 ${__("Auto Printing: Off")}`
			)
			.toggleClass("rk-autoprint-on", on)
			.toggleClass("rk-autoprint-off", !on);
	}

	drop_stations_not_automatic() {
		// A station renamed, made inactive or set to another printing method is no longer printed here.
		if (!this.print_stations.length || !Object.keys(this.print_methods).length) return;
		const gone = this.print_stations.filter((s) => this.print_methods[s] !== "Browser Automatic");
		if (!gone.length) return;
		this.set_print_stations(this.print_stations.filter((s) => !gone.includes(s)));
		frappe.show_alert({ message: __("This screen no longer prints for: {0}", [frappe.utils.escape_html(gone.join("، "))]), indicator: "orange" }, 10);
	}

	set_print_stations(stations) {
		this.print_stations = stations;
		localStorage.setItem("resto_kds_print_stations", JSON.stringify(stations));
		this.show_autoprint();
	}

	toggle_autoprint() {
		if (this.paused) {
			this.paused = false;
			this.show_autoprint();
			frappe.show_alert({ message: __("Auto Printing resumed"), indicator: "green" });
			this.claim_prints();
			return;
		}
		if (this.print_stations.length) {
			frappe.confirm(__("Stop printing new tickets on this screen? Tickets of these stations will then wait unprinted: {0}", [frappe.utils.escape_html(this.print_stations.join("، "))]), () => {
				this.set_print_stations([]);
				frappe.show_alert({ message: __("This screen no longer prints tickets by itself"), indicator: "orange" });
			});
			return;
		}
		const auto = Object.keys(this.print_methods).filter((s) => this.print_methods[s] === "Browser Automatic");
		if (!auto.length) {
			frappe.msgprint(__("No kitchen station prints automatically from the screen. Set a station's Printing Method to Browser Automatic first."));
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("This screen prints the tickets of"),
			fields: [
				{
					fieldname: "stations",
					fieldtype: "MultiCheck",
					options: auto.map((s) => ({ label: s, value: s, checked: auto.length === 1 || s === this.station })),
				},
			],
			primary_action_label: __("Start Printing"),
			primary_action: (v) => {
				const chosen = (v.stations || []).filter(Boolean);
				if (!chosen.length) return frappe.msgprint(__("Choose at least one station"));
				d.hide();
				this.set_print_stations(chosen);
				frappe.show_alert({ message: __("This screen now prints the new tickets of: {0}", [frappe.utils.escape_html(chosen.join("، "))]), indicator: "green" });
				this.refresh();
			},
		});
		d.show();
	}

	render_alerts() {
		const esc = frappe.utils.escape_html;
		const al = this.print_alerts || {};
		const parts = [];
		const count = (al.unprinted || []).length;
		// A beep when a ticket newly fails to print: the cook may not be looking at the screen.
		if (count > (this._unprinted || 0)) this.beep();
		this._unprinted = count;
		if (count) {
			const list = al.unprinted
				.map((t) => {
					const where = t.resto_table ? __("Table {0}", [esc(t.resto_table)], "Resto") : `${__(t.order_type, null, "Resto")} ${esc(t.resto_order || "")}`;
					return `${esc(t.station)}${t.daily_no ? " #" + t.daily_no : ""} (${where}${t.ticket_type === "Void" ? " · " + __("VOID") : ""})`;
				})
				.join("، ");
			parts.push(
				`<div class="rk-alert red">⚠ ${__("{0} tickets are not printed", [al.unprinted.length])} — ${list}. ${__("Check the printer and the printing screen, or press Print on the ticket.")}</div>`
			);
		}
		if ((al.no_screen || []).length) {
			parts.push(
				`<div class="rk-alert orange">⚠ ${__("No screen is printing automatically for: {0}. Turn on Auto Printing on the computer with the printer.", [esc(al.no_screen.join("، "))])}</div>`
			);
		}
		this.$alerts.html(parts.join(""));
	}

	post_waste(t) {
		// A cancelled dish the kitchen had already made: its ingredients are posted as waste.
		const open = (t.items || []).filter((d) => d.waste_open && d.order_item);
		const d = new frappe.ui.Dialog({
			title: __("Cooked: Post as Waste"),
			fields: [
				{ fieldname: "help", fieldtype: "HTML", options: `<p class="text-muted">${__("Tick the dishes the kitchen already made: their ingredients are posted as waste.")}</p>` },
				// Nothing ticked by itself: the cook says what was made.
				...resto.cooked_fields(open, (r) => `${frappe.utils.escape_html(r.item_name)} × ${r.qty}`, () => false),
			],
			primary_action_label: __("Post Waste"),
			primary_action: async (v) => {
				const lines = resto.cooked_values(open, v, (r) => r.order_item);
				if (!Object.keys(lines).length) return frappe.msgprint(__("Choose the dishes that were cooked"));
				d.hide();
				try {
					const posted = await frappe.xcall("resto.resto.floor.post_cooked_waste", { order: t.order, lines: JSON.stringify(lines) });
					// A waste that could not be posted was already shown in orange.
					if (Object.values(posted || {}).every(Boolean)) frappe.show_alert({ message: __("Waste posted"), indicator: "green" });
				} finally {
					this.refresh();
				}
			},
		});
		d.show();
	}

	async claim_prints() {
		// One ticket at a time: the server claims it (a second screen never prints it as well), the
		// browser prints it, then the screen confirms it. A ticket claimed but never confirmed (the
		// computer restarted, the network dropped) is offered again after a while, as a reprint.
		if (this._claiming) return;
		this._claiming = true;
		try {
			for (let i = 0; i < 30; i++) {
				const r = await frappe.call({
					method: "resto.resto.kitchen.kds_claim_print",
					args: { stations: JSON.stringify(this.print_stations) },
					freeze: false,
				});
				const t = r.message;
				if (!t) break;
				resto.print_window_seen = resto.print_slow = false;
				const ok = await resto.print_pages(t.pages || []);
				// Two unsure prints in a row, or a long wait, is a print window.
				if (resto.print_window_seen || (resto.print_slow && this._slow_prints)) {
					this._slow_prints = 0;
					// The print window opened: this Chrome was not started from the kitchen printing
					// shortcut. Each ticket would wait for a click, so automatic printing stops; the
					// ticket is not confirmed and shows unprinted on every kitchen screen.
					resto.print_window_seen = false;
					this.paused = true;
					this.show_autoprint();
					frappe.msgprint({
						title: __("Auto Printing paused"),
						indicator: "red",
						message: __("The print window opened, so this browser was not started from the kitchen printing shortcut. Close Chrome, open it from that shortcut, then press the Auto Printing button to resume."),
					});
					break;
				}
				if (!ok) break;
				if (resto.print_slow) {
					// Not sure it reached the printer: not confirmed, so it is offered again later marked
					// as a reprint, never lost.
					this._slow_prints = 1;
					continue;
				}
				this._slow_prints = 0;
				await frappe.call({ method: "resto.resto.kitchen.kds_printed", args: { ticket: t.name }, freeze: false });
			}
		} catch (e) {
			// The next refresh tries again.
		} finally {
			this._claiming = false;
		}
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
		.rk-sides { font-size:14px; font-weight:700; color:var(--orange-600, #c05621); }
		.rk-course { font-size:11px; background:var(--gray-100); border-radius:6px; padding:0 6px; margin-inline-start:4px; }
		.rk-actions { display:flex; gap:6px; }
		.rk-no { font-size:20px; font-weight:800; }
		.rk-printed { color:var(--green-600, #2f855a); margin-inline-start:4px; }
		.rk-unprinted { color:var(--red-600, #e53e3e); font-weight:700; margin-inline-start:4px; }
		.rk-unprinted-muted { color:var(--text-muted, #8d99a6); margin-inline-start:4px; }
		.rk-alerts:empty { display:none; }
		.rk-alert { border-radius:8px; padding:8px 12px; margin-bottom:8px; font-size:15px; font-weight:600; }
		.rk-alert.red { background:var(--red-100, #fed7d7); color:var(--red-800, #9b2c2c); border:2px solid var(--red-500, #f56565); }
		.rk-alert.orange { background:var(--orange-100, #feebc8); color:var(--orange-800, #9c4221); }
		.rk-autoprint-on { background:var(--green-500, #48bb78) !important; color:#fff !important; }
		.rk-autoprint-off { background:var(--red-500, #f56565) !important; color:#fff !important; }
		.rk-actions .btn-primary, .rk-actions .btn-danger { flex:1; padding:8px; font-size:15px; }`;
	}
}
