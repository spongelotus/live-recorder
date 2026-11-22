"""Pages for ร้านเช่ากล้อง (/shop). Mixed into web.Handler."""
import html
import json
import urllib.parse

from . import money, shop

E = html.escape


def _w():
    from . import web  # late import (web imports this module)
    return web


SHOP_CSS = """<style>
.jt{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
.jt th,.jt td{padding:5px 3px;border-top:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.35}
.jt thead th{font-weight:500;color:var(--muted);font-size:11px;border-top:0;text-align:right;padding-top:6px}
.jt .nm{text-align:left}.jt td.d{font-family:var(--mono);font-size:12px;color:var(--muted)}
.jt td.n{text-align:right;font-family:var(--mono);font-size:12px}.jt td.p{font-weight:600}
.jt .est{color:#a3a29c}.jt td.warn{color:#b2321d;font-family:var(--sans);font-size:11px}
.jt .eq{margin-left:5px}.jt .eq i{font-style:normal;font-size:10px;font-weight:500;border-radius:5px;padding:1px 5px;margin-right:3px}
.q-cam{background:#e1ddff;color:#3924ba}.q-l100{background:#ffd6f1;color:#a3127a}.q-l70{background:#ffdcc9;color:#c2410c}
.jt tr.self .eq i,.jt tr.fut .eq i{opacity:.55}
.jt .tg{font-size:10.5px;background:var(--soft2);border-radius:6px;padding:1px 5px;margin-right:4px}.jt .tg.warn{background:#ffeec2;color:#7a5200}
.jt tr.self td,.jt tr.fut td{color:#a3a29c}.jt tr.self td.nm{text-decoration:none}
.jt tbody tr{cursor:pointer}.jt tbody tr:hover td{background:var(--soft)}
.jt tfoot td{font-weight:600;font-size:12px;border-top:1.5px solid var(--ink2)}
.mchart{display:flex;align-items:flex-end;gap:4px;height:170px;padding-top:4px}
.mb{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.mb i{display:block;width:100%;max-width:38px}.mb i.p{background:#1c1c1c;border-radius:0 0 3px 3px;min-height:1px}
.mb i.s{background:var(--shop);border-radius:5px 5px 0 0}
.mb b{font-size:10px;font-weight:500;font-family:var(--mono);color:var(--ink2);margin-bottom:2px}
.mb span{font-size:10px;color:var(--muted);margin-top:4px;white-space:nowrap}.mb small{font-size:9px;color:#a3a29c;font-family:var(--mono)}
.mb:hover i{opacity:.75}
</style>"""


def baht(v):
    return money.fmt(round(v))


class ShopPages:
    # ---------- GET /shop ----------
    def shop_page(self):
        w = _w()
        q = self.query()
        if (q.get("sync") or [""])[0]:
            shop.sync(force=True)
        else:
            shop.sync()
        d = shop.load()
        md = money.load()
        js = shop.jobs(d, md)
        s = shop.summary(d, js)
        parts = [self.flash({"saved": "บันทึกแล้ว", "synced": "ดึงชีตคิวใหม่แล้ว", "imported": "นำเข้าข้อมูลแล้ว"})]
        if d.get("sync_error"):
            parts.append(f'<div class="card note">{E(d["sync_error"])}</div>')
        if not d["sheet_id"]:
            parts.append('<div class="card note">ใส่ลิงก์ชีตคิว (Google Sheets) ในหัวข้อ “ตั้งค่าร้าน” ด้านล่างก่อน '
                         'ระบบจะดึงงานเช่าทั้งหมดมาให้เอง</div>')

        # ---- payback summary ----
        pct = min(100.0, s["pct"])
        parts.append(
            f'<div class="card dark"><div class="muted">คืนทุนแล้ว</div>'
            f'<div class="big">{baht(s["profit"])}<small>/ {baht(s["cost"])}</small></div>'
            f'<div class="barc" style="height:10px;background:rgba(255,255,255,.12)"><i style="width:{pct:.1f}%"></i></div>'
            f'<div class="stats" style="margin-top:12px">'
            f'<div class="stat"><b>{s["pct"]:.0f}%</b><span>คืนทุนแล้ว</span></div>'
            f'<div class="stat"><b>{baht(s["left"])}</b><span>เหลืออีก</span></div>'
            f'<div class="stat"><b>{baht(s["pace"])}</b><span>กำไรเฉลี่ย/เดือน (3 เดือนล่าสุด)</span></div>'
            f'<div class="stat"><b class="clip" style="font-size:20px">{E(s["eta"] or "-")}</b><span>คาดว่าคืนทุนครบ</span></div>'
            f'<div class="stat"><b>{s["n"]}</b><span>งานให้เช่า · ใช้เอง {s["n_self"]}</span></div>'
            f'<div class="stat"><b>{baht(s["avg_ship"])}</b><span>ค่าส่งเฉลี่ย/งาน</span></div>'
            f'</div></div>')

        # ---- per equipment ----
        rows = []
        for e in shop.EQUIP:
            eq = d["equipment"][e]
            cost = float(eq.get("cost") or 0)
            got = s["per_eq"][e]
            p = (got / cost * 100) if cost else 0
            rows.append(f'<div class="row" style="display:block"><div class="row plain"><span>{E(eq["name"])}</span>'
                        f'<span class="mono">{baht(got)} / {baht(cost)} · <b>{p:.0f}%</b></span></div>'
                        f'<div class="barc"><i style="width:{min(100, p):.0f}%"></i></div></div>')
        parts.append('<h2>คืนทุนแยกอุปกรณ์ <span class="muted" style="font-weight:400">· งานคอมโบแบ่งกำไรตามราคาเช่าเดี่ยว</span></h2>'
                     f'<div class="card list">{"".join(rows)}</div>')

        # ---- edit one job ----
        edit = (q.get("edit") or [""])[0]
        ej = next((j for j in js if j["key"] == edit), None)
        if ej:
            edays = f" · {ej['days']} วัน" if ej["days"] > 1 else ""
            parts.append(f'<div class="card" id="edit"><div class="row plain"><div><span class="title">{E(ej["name"])}</span>'
                         f'<div class="muted">{E(shop.thai_date(ej["date"]))}{edays}</div></div>'
                         f'<a class="btn gray sm" href="/shop#m-{ej["date"][:7]}">✕</a></div>{self.shop_form(ej, d, md)}</div>')

        # ---- things to check ----
        todo = [j for j in js if j["needs_link"] or (j["guess_self"] and not j["future"])]
        if todo:
            parts.append(f'<h2>รอเช็ก · {len(todo)} <span class="muted" style="font-weight:400">· แตะเพื่อแก้</span></h2>'
                         f'<div class="card list" style="padding:4px 12px">{self.shop_table(todo, total=False)}</div>')

        # ---- monthly chart ----
        if s["months"]:
            parts.append(f'<h2>สรุปรายเดือน</h2><div class="card">{self.shop_chart(s["months"])}</div>')

        # ---- all jobs by month ----
        months = []
        for j in js:
            if not months or months[-1][0] != j["date"][:7]:
                months.append((j["date"][:7], []))
            months[-1][1].append(j)
        for m, items in months:
            y, mo = int(m[:4]), int(m[5:7])
            parts.append(f'<h2 id="m-{m}">{shop.TH_M[mo]} {y + 543}</h2>'
                         f'<div class="card list" style="padding:4px 12px">{self.shop_table(items)}</div>')
        parts.append('<p class="muted" style="margin:6px 4px">สีเทา = ตัวเลขประมาณ (ยังไม่มีรายการเงินจริง) · แตะแถวเพื่อแก้ไข / ผูกรายการเงิน</p>')
        parts.append(self.shop_settings(d))
        sub = f'{len(todo)} รอเช็ก' if todo else ""
        self.send_html(w.page("ร้านกล้อง", SHOP_CSS + "".join(parts), active="/shop", heading="ร้านเช่ากล้อง", sub=sub))

    def shop_table(self, js, total=True):
        def n(v, actual=True, dash=False):
            if dash:
                return '<td class="n">-</td>'
            return f'<td class="n{"" if actual else " est"}">{money.fmt(round(v)).replace("฿", "")}</td>'
        rows = []
        for j in js:
            items = "".join(f'<i class="q-{x}">{shop.CODE[x]}</i>' for x in j["items"]) or "?"
            cls, tag = "", ""
            if j["status"] == "self":
                cls, tag = "self", '<span class="tg">ใช้เอง</span>'
            elif j["future"]:
                cls, tag = "fut", '<span class="tg">จอง</span>'
            elif j["guess_self"]:
                tag = '<span class="tg warn">ใช้เอง?</span>'
            days = f'×{j["days"]}' if j["days"] > 1 else ""
            href = "/shop?edit=" + urllib.parse.quote(j["key"]) + "#edit"
            dd = j["date"]
            first = (f'<td class="d">{int(dd[8:10])}/{int(dd[5:7])}</td>'
                     f'<td class="nm">{tag}{E(j["name"])}<span class="eq">{items}{days}</span></td>')
            if j["status"] == "self":
                cells = n(0, dash=True) * 4
            else:
                rent = (f'<td class="n warn">รอโอน</td>' if j["needs_link"] else n(j["revenue"], j["price_actual"] and not j["future"]))
                sa = j["ship_actual"] and not j["future"]
                cells = (rent + n(j["ship_out"], sa) + n(j["ship_back"], sa)
                         + f'<td class="n p{"" if (j["price_actual"] and sa) else " est"}">{money.fmt(round(j["profit"])).replace("฿", "")}</td>')
            rows.append(f'<tr class="{cls}" onclick="location.href=\'{href}\'">{first}{cells}</tr>')
        foot = ""
        if total:
            c = [j for j in js if j["status"] == "rent" and not j["future"]]
            if c:
                foot = (f'<tfoot><tr><td></td><td class="nm">รวม {len(c)} งาน</td>'
                        + "".join(f'<td class="n">{money.fmt(round(sum(j[k] for j in c))).replace("฿", "")}</td>'
                                  for k in ("revenue", "ship_out", "ship_back", "profit")) + '</tr></tfoot>')
        return ('<table class="jt"><colgroup><col style="width:46px"><col><col style="width:52px"><col style="width:44px">'
                '<col style="width:44px"><col style="width:54px"></colgroup>'
                '<thead><tr><th>วันที่</th><th class="nm">งาน</th><th>ค่าเช่า</th><th>ไป</th><th>กลับ</th><th>กำไร</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody>{foot}</table>')

    def shop_chart(self, months):
        keys = sorted(months)
        # fill gaps so empty months show as zero
        allk = []
        y, m = int(keys[0][:4]), int(keys[0][5:7])
        while f"{y}-{m:02d}" <= keys[-1]:
            allk.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                y, m = y + 1, 1
        top = max([months[k]["rev"] for k in keys] + [1])
        bars = []
        for k in allk:
            x = months.get(k, {"rev": 0, "ship": 0, "profit": 0, "n": 0})
            hp = max(0, x["profit"]) / top * 100
            hs = x["ship"] / top * 100
            lbl = f'{x["profit"] / 1000:.1f}k' if x["profit"] else ""
            bars.append(f'<a class="mb" href="#m-{k}" title="{shop.TH_M[int(k[5:7])]} · {x["n"]} งาน · รับ {baht(x["rev"])} · ส่ง {baht(x["ship"])} · กำไร {baht(x["profit"])}">'
                        f'<b>{lbl}</b><i class="s" style="height:{hs:.1f}%"></i><i class="p" style="height:{hp:.1f}%"></i>'
                        f'<span>{shop.TH_M[int(k[5:7])]}</span><small>{x["n"] or ""}</small></a>')
        return (f'<div class="mchart">{"".join(bars)}</div>'
                '<div class="davg"><span><i class="dot" style="background:#1c1c1c"></i>กำไร</span>'
                '<span><i class="dot" style="background:var(--shop)"></i>ค่าส่ง (กำไร + ค่าส่ง = ค่าเช่า)</span>'
                '<span class="muted">ตัวเลขใต้เดือน = จำนวนงาน</span></div>')

    def shop_form(self, j, d, md):
        meta = j["meta"]
        used = shop.used_links(d, j["key"])
        cands = shop.candidates(j, md, used)
        linked = set(meta.get("links", [])) if "links" in meta else {x["id"] for x in j["links"]}
        boxes = []
        for it in cands:
            k = it.get("kind")
            sign = "+" if k in ("income", "refund") else "-"
            title = it.get("merchant") or it.get("note") or it.get("category") or ""
            note = f' · {E(it["note"])}' if it.get("note") and it.get("note") != title else ""
            boxes.append(f'<label style="display:flex;gap:4px;align-items:flex-start;font-weight:400;margin:6px 0">'
                         f'<input type="checkbox" name="link_{E(it["id"])}" value="1"{" checked" if it["id"] in linked else ""}>'
                         f'<span><span class="mono">{E(it["time"][8:10])}/{E(it["time"][5:7])} {E(it["time"][11:16])}</span> '
                         f'<b class="mono" style="font-weight:500">{sign}{money.fmt(float(it["amount"]))}</b> {E(title)}'
                         f'<span class="muted">{note}</span></span></label>')
        boxes_html = "".join(boxes) or '<div class="muted">ไม่มีรายการเงินช่วงวันนี้ (±10 วัน) ให้เลือก</div>'
        eq_boxes = "".join(f'<label style="display:inline-flex;align-items:center;margin:4px 12px 0 0;font-weight:400">'
                           f'<input type="checkbox" name="eq_{e}" value="1"{" checked" if e in j["items"] else ""}>{shop.SHORT[e]}</label>'
                           for e in shop.EQUIP)
        st = j["status"]
        half = float(d["ship_default"]) / 2

        def v(k):
            return E(str(meta.get(k, "")))
        return f'''<form method="post" action="/shop/job" style="margin-top:8px">
<input type="hidden" name="key" value="{E(j["key"])}">
<div class="seg2"><label><input type="radio" name="status" value="rent"{" checked" if st == "rent" else ""}><span>ให้เช่า</span></label>
<label><input type="radio" name="status" value="self"{" checked" if st == "self" else ""}><span>ใช้เอง</span></label></div>
<label>อุปกรณ์ที่เช่า</label><div>{eq_boxes}</div>
<label>รายการเงินของงานนี้ (เงินเข้า = ค่าเช่า · GrabExpress “ส่งให้” = ไป · “ส่งคืน/ส่งถึงบัว” = กลับ)</label>{boxes_html}
<label>ใส่ตัวเลขเอง (ถ้ากรอก จะใช้แทนรายการเงินที่ติ๊ก · เว้นว่าง = ใช้จากรายการเงิน)</label>
<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 8px">
<div><label>ค่าเช่า</label><input name="price" type="number" step="1" value="{v("price")}" placeholder="{shop.table_price(d, j["items"]) * j["days"]:g}"></div>
<div><label>ส่งไป</label><input name="ship_out" type="number" step="1" value="{v("ship_out")}" placeholder="{half:g}"></div>
<div><label>ส่งกลับ</label><input name="ship_back" type="number" step="1" value="{v("ship_back")}" placeholder="{half:g}"></div></div>
<label>โน้ต</label><input name="note" value="{v("note")}">
<div class="actions"><button>บันทึก</button></div></form>'''

    def shop_settings(self, d):
        eq = "".join(f'<div><label>ราคาทุน {E(d["equipment"][e]["name"])}</label>'
                     f'<input name="cost_{e}" type="number" step="1" value="{float(d["equipment"][e].get("cost") or 0):g}"></div>'
                     for e in shop.EQUIP)
        names = {"cam": "เฉพาะกล้อง", "l100": "เฉพาะเลนส์ 100-400", "l70": "เฉพาะเลนส์ 70-200",
                 "cam+l100": "กล้อง + 100-400", "cam+l70": "กล้อง + 70-200"}
        pr = "".join(f'<div><label>{n} (บาท/วัน)</label><input name="price_{k.replace("+", "_")}" type="number" step="1" '
                     f'value="{float(d["prices"].get(k, 0)):g}"></div>' for k, n in names.items())
        sheet = f'https://docs.google.com/spreadsheets/d/{d["sheet_id"]}' if d["sheet_id"] else ""
        import time as _t
        synced = _t.strftime("%d/%m %H:%M", _t.localtime(d["synced_at"])) if d.get("synced_at") else "-"
        return f'''<details class="fold"><summary>ตั้งค่าร้าน</summary><div class="card">
<form method="post" action="/shop/settings">
<label style="margin-top:0">ลิงก์ชีตคิว (Google Sheets ที่แชร์แบบ “ทุกคนที่มีลิงก์ดูได้”)</label>
<input name="sheet" value="{E(sheet)}" placeholder="https://docs.google.com/spreadsheets/d/...">
<div class="muted" style="margin-top:4px">คอลัมน์ A วันที่ · B ชื่องาน · C กล้อง · D เลนส์ 100-400 · E เลนส์ 70-200 (ระบายแดง = เช่า) · ดึงล่าสุด {E(synced)}</div>
<div class="two"><div><label>ค่าส่งไปกลับโดยประมาณ (บาท/งาน)</label><input name="ship_default" type="number" step="1" value="{float(d["ship_default"]):g}"></div>
<div><label>ผูกรายการเงินจริงตั้งแต่วันที่</label><input name="actual_from" type="date" value="{E(d.get("actual_from") or "")}"></div></div>
<div class="two">{eq}</div>
<div class="two">{pr}</div>
<div class="actions"><button>บันทึก</button><a class="btn gray" href="/shop?sync=1&msg=synced">ดึงชีตคิวใหม่ตอนนี้</a></div>
</form><p class="muted">งานที่วันเดียวกับอัลบั้มใน spongelotus.work จะขึ้นป้าย “ใช้เอง?” ให้ยืนยัน · ระบบไม่แก้อะไรในชีตหรือแกลเลอรี</p></div></details>'''

    # ---------- POST ----------
    def shop_post(self, path, f):
        if path == "/shop/job":
            key = f.get("key", "")

            def apply(d):
                if not any(r["key"] == key for r in d["rows"]):
                    return
                m = d["jobs"].setdefault(key, {})
                m["status"] = "self" if f.get("status") == "self" else "rent"
                m.pop("ship", None)
                for fld in ("price", "ship_out", "ship_back"):
                    v = (f.get(fld) or "").strip()
                    try:
                        m[fld] = round(float(v), 2) if v != "" else ""
                    except ValueError:
                        pass
                m["note"] = (f.get("note") or "")[:120]
                m["links"] = [k[5:] for k, v in f.items() if k.startswith("link_") and v]
                row = next(r for r in d["rows"] if r["key"] == key)
                items = [e for e in shop.EQUIP if f.get("eq_" + e)]
                m["items"] = items if items != row["items"] else []
            shop.update(apply)
            return self.redirect("/shop?msg=saved#m-" + key[:7])
        if path == "/shop/settings":
            def apply(d):
                sid = shop.sheet_id_from(f.get("sheet", ""))
                if sid != d["sheet_id"]:
                    d["synced_at"] = 0
                d["sheet_id"] = sid
                try:
                    d["ship_default"] = max(0.0, float(f.get("ship_default") or 0))
                except ValueError:
                    pass
                af = (f.get("actual_from") or "").strip()
                d["actual_from"] = af if len(af) == 10 else ""
                for e in shop.EQUIP:
                    try:
                        d["equipment"][e]["cost"] = max(0.0, float(f.get("cost_" + e) or 0))
                    except ValueError:
                        pass
                for k in list(d["prices"]):
                    try:
                        d["prices"][k] = max(0.0, float(f.get("price_" + k.replace("+", "_")) or 0))
                    except ValueError:
                        pass
            shop.update(apply)
            shop.sync(force=True)
            return self.redirect("/shop?msg=saved")
        self.send_html("not found", 404)

    def shop_import(self):
        """POST JSON {settings:{...}, jobs:[{date, name?, status?, price?, ship?, note?}]} — one-off backfill."""
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 500_000)).decode("utf-8"))
        except Exception:
            return self.send_html("bad json", 400)
        if body.get("sync"):
            shop.sync(force=True)
        report = {"matched": 0, "missing": []}

        def apply(d):
            st = body.get("settings") or {}
            for k in ("sheet_id", "ship_default", "actual_from"):
                if k in st:
                    d[k] = st[k]
            for e, v in (st.get("equipment") or {}).items():
                if e in d["equipment"]:
                    d["equipment"][e].update({kk: vv for kk, vv in v.items() if kk in ("name", "cost")})
            for k, v in (st.get("prices") or {}).items():
                d["prices"][k] = float(v)
            for x in body.get("jobs") or []:
                cand = [r for r in d["rows"] if r["date"] == x.get("date")]
                if x.get("name"):
                    cand = [r for r in cand if x["name"].lower() in r["name"].lower()] or cand
                if len(cand) != 1:
                    report["missing"].append(x)
                    continue
                m = d["jobs"].setdefault(cand[0]["key"], {})
                for k in ("status", "price", "ship", "ship_out", "ship_back", "note"):
                    if k in x:
                        m[k] = x[k]
                report["matched"] += 1
        shop.update(apply)
        data = json.dumps(report, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def shop_card(self):
        w = _w()
        d = shop.load()
        if not d["sheet_id"]:
            return ""
        js = shop.jobs(d)
        s = shop.summary(d, js)
        nxt = [j for j in js if j["future"]]
        nxt_line = (f'<div class="row"><span>งานถัดไป</span><b class="clip">{E(shop.thai_date(nxt[-1]["date"]))} · {E(nxt[-1]["name"][:22])}</b></div>'
                    if nxt else "")
        this_m = money.now().strftime("%Y-%m")
        mm = s["months"].get(this_m, {"n": 0, "profit": 0})
        return (f'<a class="card k-shop" href="/shop"><div class="chead"><div class="ctitle">'
                f'<span class="ico">{w.icon("camera")}</span><span>ร้านกล้อง<small>คืนทุน {s["pct"]:.0f}%</small></span></div></div>'
                f'<div class="big">{baht(s["profit"])}<small>/ {baht(s["cost"])}</small></div>'
                f'<div class="barc"><i style="width:{min(100, s["pct"]):.0f}%"></i></div>'
                f'<div class="row" style="margin-top:6px"><span>เดือนนี้</span><b>{mm["n"]} งาน · {baht(mm["profit"])}</b></div>{nxt_line}</a>')
