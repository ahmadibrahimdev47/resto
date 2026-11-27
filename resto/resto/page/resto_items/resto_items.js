// The items at a glance (D-288): picture, name, group, unit, selling price, what each is used for and
// whether it is active. Read only: an item opens in its own form.

frappe.pages["resto-items"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Items"), single_column: true });
	wrapper.resto_items = new RestoItems(page);
};

frappe.pages["resto-items"].on_page_show = function (wrapper) {
	// Back from an item's form: what was changed there shows.
	wrapper.resto_items && wrapper.resto_items.loaded && wrapper.resto_items.load();
};

class RestoItems {
	constructor(page) {
		this.page = page;
		this.items = [];
		page.add_inner_button(__("Item List", null, "Resto Items"), () => frappe.set_route("List", "Item"));
		const esc = frappe.utils.escape_html;
		const opt = (v, l) => `<option value="${esc(v)}">${esc(l)}</option>`;
		$(page.body).html(`<style>${RestoItems.css()}</style>
			<div class="ri-filters">
				<input type="search" class="form-control input-sm ri-search" placeholder="${esc(__("Search by name or code", null, "Resto Items"))}">
				<select class="form-control input-sm ri-group">${opt("", __("All Groups", null, "Resto Items"))}</select>
				<select class="form-control input-sm ri-tag">
					${opt("", __("All Uses", null, "Resto Items"))}
					${opt("sales", __("Sales", null, "Resto Items"))}
					${opt("purchase", __("Purchases", null, "Resto Items"))}
					${opt("stock", __("Stock Item", null, "Resto Items"))}
					${opt("prep", __("Prepared", null, "Resto Items"))}
				</select>
				<select class="form-control input-sm ri-status">
					${opt("active", __("Active", null, "Resto Items"))}
					${opt("disabled", __("Disabled", null, "Resto Items"))}
					${opt("", __("All", null, "Resto Items"))}
				</select>
			</div>
			<div class="ri-grid"></div>
			<div class="ri-more"><button class="btn btn-default btn-sm">${esc(__("Show More", null, "Resto Items"))}</button></div>`);
		this.$body = $(page.body);
		this.$grid = this.$body.find(".ri-grid");
		this.$more = this.$body.find(".ri-more").hide();
		this.$more.find("button").on("click", () => this.load(true));
		let timer;
		this.$body.find(".ri-search").on("input", () => {
			clearTimeout(timer);
			timer = setTimeout(() => this.load(), 300);
		});
		this.$body.find(".ri-group, .ri-tag, .ri-status").on("change", () => this.load());
		this.$grid.on("click", "[data-open]", (e) => frappe.set_route("Form", "Item", $(e.currentTarget).attr("data-open")));
		frappe.call("resto.resto.items.get_groups").then((r) => {
			this.$body.find(".ri-group").append((r.message || []).map((g) => opt(g, __(g))).join(""));
		});
		this.load();
	}

	filters() {
		const b = this.$body;
		return {
			search: b.find(".ri-search").val(),
			item_group: b.find(".ri-group").val(),
			tag: b.find(".ri-tag").val(),
			status: b.find(".ri-status").val(),
		};
	}

	load(more) {
		const seq = (this.seq = (this.seq || 0) + 1);
		frappe
			.call("resto.resto.items.get_items", { ...this.filters(), start: more ? this.items.length : 0 })
			.then((r) => {
				if (seq !== this.seq) return; // a newer search is on its way
				const d = r.message;
				this.loaded = true;
				this.items = more ? this.items.concat(d.items) : d.items;
				this.currency = d.currency;
				this.can_write = d.can_write;
				if (!this.new_button && d.can_create) {
					this.new_button = this.page.set_primary_action(__("New Item", null, "Resto Items"), () => frappe.new_doc("Item"), "add");
				}
				this.render();
				this.$more.toggle(!!d.more);
			});
	}

	render() {
		const esc = frappe.utils.escape_html;
		if (!this.items.length) {
			this.$grid.html(`<div class="ri-empty text-muted">${esc(__("No item matches", null, "Resto Items"))}</div>`);
			return;
		}
		const action = this.can_write ? __("Edit", null, "Resto Items") : __("Open", null, "Resto Items");
		this.$grid.html(
			this.items
				.map((it) => {
					const tags = [
						it.is_sales_item && __("Sales", null, "Resto Items"),
						it.is_purchase_item && __("Purchases", null, "Resto Items"),
						it.is_stock_item && __("Stock Item", null, "Resto Items"),
						it.default_bom && __("Prepared", null, "Resto Items"),
						it.has_variants && __("Has Variants", null, "Resto Items"),
						it.variant_of && __("Variant", null, "Resto Items"),
					].filter(Boolean);
					const price =
						it.price == null
							? `<span class="text-muted">—</span>`
							: `${format_currency(it.price, this.currency)}${it.price_uom ? ` <span class="text-muted">/ ${esc(__(it.price_uom))}</span>` : ""}`;
					const picture = it.image
						? `<img src="${esc(encodeURI(it.image))}" alt="" loading="lazy">`
						: `<span class="ri-letter">${esc((it.item_name || it.name).trim().charAt(0))}</span>`;
					const subtitle = it.item_name && it.item_name !== it.name ? `<div class="ri-code">${esc(it.name)}</div>` : "";
					return `<div class="ri-card ${it.disabled ? "ri-off" : ""}">
						<div class="ri-pic" data-open="${esc(it.name)}">${picture}</div>
						<div class="ri-main">
							<div class="ri-head">
								<div class="ri-name" data-open="${esc(it.name)}">${esc(it.item_name || it.name)}</div>
								<span class="ri-status ${it.disabled ? "off" : "on"}">${esc(
									it.disabled ? __("Disabled", null, "Resto Items") : __("Active", null, "Resto Items")
								)}</span>
							</div>
							${subtitle}
							<div class="ri-line">
								<span class="ri-chip">${esc(__(it.item_group || ""))}</span>
								<span class="text-muted">${esc(__(it.stock_uom || ""))}</span>
								<span class="ri-price">${price}</span>
							</div>
							<div class="ri-line ri-tags">${tags.map((t) => `<span class="ri-tag">${esc(t)}</span>`).join("")}</div>
						</div>
						<button class="btn btn-default btn-xs ri-act" data-open="${esc(it.name)}">${esc(action)}</button>
					</div>`;
				})
				.join("")
		);
	}

	static css() {
		return `
		.ri-filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
		.ri-filters .ri-search { flex:1 1 220px; min-width:180px; }
		.ri-filters select { flex:0 1 170px; min-width:130px; }
		.ri-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); gap:10px; }
		@media (max-width: 600px) { .ri-grid { grid-template-columns:1fr; } }
		.ri-card { display:flex; gap:12px; align-items:flex-start; border:1px solid var(--border-color); border-radius:10px;
			padding:10px; background:var(--card-bg); }
		.ri-card.ri-off { opacity:.65; }
		.ri-pic { flex:none; width:64px; height:64px; border-radius:8px; overflow:hidden; background:var(--control-bg);
			display:flex; align-items:center; justify-content:center; cursor:pointer; }
		.ri-pic img { width:100%; height:100%; object-fit:cover; }
		.ri-letter { font-size:26px; font-weight:700; color:var(--text-muted); }
		.ri-main { flex:1; min-width:0; }
		.ri-head { display:flex; align-items:center; gap:8px; justify-content:space-between; }
		.ri-name { font-weight:700; font-size:14px; cursor:pointer; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
		.ri-code { font-size:11px; color:var(--text-muted); }
		.ri-line { display:flex; flex-wrap:wrap; align-items:center; gap:6px 10px; margin-top:6px; font-size:12px; }
		.ri-chip { background:var(--bg-blue, #e8f1fd); color:var(--text-on-blue, #1a5bb5); border-radius:999px; padding:1px 9px; }
		.ri-price { font-weight:700; margin-inline-start:auto; white-space:nowrap; }
		.ri-tag { border:1px solid var(--border-color); border-radius:6px; padding:0 7px; color:var(--text-muted); font-size:11px; }
		.ri-status { flex:none; border-radius:999px; padding:1px 9px; font-size:11px; }
		.ri-status.on { background:var(--bg-green, #e4f5e9); color:var(--text-on-green, #1f7a3d); }
		.ri-status.off { background:var(--bg-gray, #eee); color:var(--text-muted); }
		.ri-act { flex:none; align-self:center; }
		.ri-empty { padding:40px; text-align:center; grid-column:1 / -1; }
		.ri-more { text-align:center; margin:14px 0; }`;
	}
}
