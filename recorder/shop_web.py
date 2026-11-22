"""Pages for ร้านเช่ากล้อง (/shop). Mixed into web.Handler."""
import html
import json
import urllib.parse

from . import money, shop

E = html.escape


def _w():
    from . import web  # late import (web imports this module)
    return web


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

        # ---- things to check ----
        todo = [j for j in js if j["needs_link"] or (j["guess_self"] and not j["future"])]
        if todo:
            rows = "".join(self.shop_job(j, d, md) for j in todo)
            parts.append(f'<h2>รอเช็ก · {len(todo)} <span class="muted" style="font-weight:400">· '
                         f'ไม่เจอยอดโอนที่ตรงราคา หรือยืนยันว่าใช้เอง</span></h2><div class="card list">{rows}</div>')

        # ---- months ----
        if s["months"]:
            rows = []
            for m in sorted(s["months"], reverse=True):
                x = s["months"][m]
                y, mo = int(m[:4]), int(m[5:7])
                est = ' <span class="pill">ประมาณ</span>' if x["est"] else ""
                rows.append(f'<a class="row" href="#m-{m}"><div><span class="title">{shop.TH_M[mo]} {y + 543}</span>{est}'
                            f'<div class="muted">{x["n"]} งาน · รับ {baht(x["rev"])} · ค่าส่ง {baht(x["ship"])}</div></div>'
                            f'<div class="price">{baht(x["profit"])}<small>กำไร</small></div></a>')
            parts.append(f'<h2>สรุปรายเดือน</h2><div class="card list">{"".join(rows)}</div>')

        # ---- all jobs by month ----
        cur = None
        buf = []
        for j in js + [None]:
            m = j["date"][:7] if j else None
            if m != cur and buf:
                y, mo = int(cur[:4]), int(cur[5:7])
                parts.append(f'<h2 id="m-{cur}">{shop.TH_M[mo]} {y + 543}</h2><div class="card list">{"".join(buf)}</div>')
                buf = []
            if j is None:
                break
            cur = m
            buf.append(self.shop_job(j, d, md))
        parts.append(self.shop_settings(d))
        sub = f'{len(todo)} รอเช็ก' if todo else ""
        self.send_html(w.page("ร้านกล้อง", "".join(parts), active="/shop", heading="ร้านเช่ากล้อง", sub=sub))

    def shop_job(self, j, d, md):
        meta = j["meta"]
        items = "".join(f'<span class="pill">{shop.SHORT[x]}</span> ' for x in j["items"]) or '<span class="pill err">ไม่มีอุปกรณ์</span>'
        days = f' · {j["days"]} วัน' if j["days"] > 1 else ""
        tags = ""
        if j["status"] == "self":
            tags += ' <span class="pill">ใช้เอง</span>'
        elif j["guess_self"] and not j["future"]:
            tags += ' <span class="pill err">มีอัลบั้มวันนี้ · ใช้เอง?</span>'
        if j["future"]:
            tags += ' <span class="pill acc">จองแล้ว</span>'
        if j["needs_link"]:
            tags += ' <span class="pill err">ไม่เจอยอดโอน</span>'
        elif j["auto"]:
            tags += ' <span class="pill ok">จับคู่อัตโนมัติ</span>'
        if j["status"] == "self":
            price = '<span class="muted">-</span>'
        else:
            est = "" if (j["price_actual"] and j["ship_actual"]) else "≈"
            price = (f'{est}{baht(j["profit"])}<small>รับ {baht(j["revenue"])} · ส่ง {baht(j["ship"])}</small>')
        used = shop.used_links(d, j["key"])
        cands = shop.candidates(j, md, used)
        linked = set(meta.get("links", []))
        if "links" not in meta:
            linked = {x["id"] for x in j["links"]}  # automatic match (saved only after "บันทึก")
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
        boxes_html = ("".join(boxes) if boxes else
                      '<div class="muted">ไม่มีรายการเงินช่วงวันนี้ (±10 วัน) ให้เลือก</div>')

        def chk(eq):
            return " checked" if eq in j["items"] else ""
        eq_boxes = "".join(f'<label style="display:inline-flex;align-items:center;margin:4px 12px 0 0;font-weight:400">'
                           f'<input type="checkbox" name="eq_{e}" value="1"{chk(e)}>{shop.SHORT[e]}</label>' for e in shop.EQUIP)
        st = j["status"]
        form = f'''<form method="post" action="/shop/job" style="margin-top:8px">
<input type="hidden" name="key" value="{E(j["key"])}">
<div class="seg2"><label><input type="radio" name="status" value="rent"{" checked" if st == "rent" else ""}><span>ให้เช่า</span></label>
<label><input type="radio" name="status" value="self"{" checked" if st == "self" else ""}><span>ใช้เอง</span></label></div>
<label>อุปกรณ์ที่เช่า (ตามช่องสีแดงในชีต)</label><div>{eq_boxes}</div>
<label>เงินที่เกี่ยวกับงานนี้ (ติ๊ก = ผูกกับงาน · เงินเข้า = ค่าเช่า · รายจ่าย = ค่าส่ง)</label>{boxes_html}
<div class="two"><div><label>ค่าเช่า (ถ้าไม่ผูกเงิน)</label><input name="price" type="number" step="1" value="{E(str(meta.get("price", "")))}" placeholder="{shop.table_price(d, j["items"]) * j["days"]:g}"></div>
<div><label>ค่าส่งไปกลับ (ถ้าไม่ผูก)</label><input name="ship" type="number" step="1" value="{E(str(meta.get("ship", "")))}" placeholder="{float(d["ship_default"]):g}"></div></div>
<label>โน้ต</label><input name="note" value="{E(meta.get("note", ""))}">
<div class="actions"><button>บันทึก</button></div></form>'''
        note = f' · {E(meta["note"])}' if meta.get("note") else ""
        return (f'<details class="row" style="display:block"><summary style="list-style:none;cursor:pointer;margin:0;color:inherit;font-size:inherit">'
                f'<div class="row plain" style="display:flex;justify-content:space-between;gap:12px;align-items:center"><div style="min-width:0">'
                f'<span class="title">{E(j["name"])}</span>{tags}'
                f'<div class="muted line2"><span class="mono">{E(shop.thai_date(j["date"]))}</span>{days} · {items}{note}</div></div>'
                f'<div class="price">{price}</div></div></summary>{form}</details>')

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
                for fld in ("price", "ship"):
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
            return self.redirect("/shop?msg=saved")
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
                for k in ("status", "price", "ship", "note"):
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
