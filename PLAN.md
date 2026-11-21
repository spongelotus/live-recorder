# PLAN — ระบบผู้ช่วยส่วนตัว (Bua)

> เริ่ม session ใหม่: บอก Claude ว่า "อ่าน PLAN.md ก่อน" แล้วค่อยสั่งงาน
> เจ้าของเขียนโค้ดไม่เป็น — Claude เขียนให้ทั้งหมด ถ้าต้องรันคำสั่งบนเซิร์ฟเวอร์ ให้บอกทีละขั้นแบบ copy ไปวางได้

## ภาพรวม
ระบบเดียวที่รวม "ผู้ช่วย" หลายตัวไว้หลังประตูเดียว (ลิงก์เดิม + รหัสผ่านเดิม)
รันบน Oracle Cloud (Always Free, Singapore) · Python ล้วน ไม่มี dependency นอกจาก ffmpeg/streamlink ของ recorder
แจ้งเตือนทุกอย่างผ่าน ntfy topic เดียวกัน

```
https://<ip>.sslip.io  (Caddy ทำ https → 127.0.0.1:8080)
  /           Hub — การ์ดสรุปทุกส่วน
  /recorder   อัดไลฟ์ TikTok + Instagram
  /subs       Subscription tracker
  /settings   ตั้งค่า recorder + รหัสผ่าน + ntfy
  /log        บันทึกระบบ
  /likes      YouTube like → ntfy → แตะแล้วโพสต์ X เอง (ฟรี ไม่ใช้ X API)
```

## โค้ด
| ไฟล์ | หน้าที่ |
|---|---|
| `recorder/main.py` | ลูปหลัก: เช็กไลฟ์, cleanup ทุกชม., เตือน subscription ทุกชม., self-restart หลังอัปเดต |
| `recorder/web.py` | หน้าเว็บทั้งหมด + login (cookie `tkrec`, 30 วัน) |
| `recorder/subs.py` | ข้อมูล subscription, ยอดรวม, เลื่อนวันต่ออายุ, ส่งเตือน |
| `recorder/likes.py` | เชื่อม YouTube (OAuth device flow), เช็กเพลย์ลิสต์ LL ทุก N นาทีใน thread แยก, ส่ง ntfy พร้อมลิงก์ x.com/intent/post |
| `recorder/tiktok.py` / `instagram.py` / `record.py` | ตรวจไลฟ์และอัด |
| `recorder/notify.py` | ส่ง ntfy |
| `deploy/` | systemd services, ตัวอัปเดตอัตโนมัติ (ดึง GitHub ทุก 3 ชม.), คำสั่ง `liverec` |

## ข้อมูล (อยู่บนเซิร์ฟเวอร์เท่านั้น — repo นี้เป็น public)
`/var/lib/live-recorder/`
- `settings.json` — รหัสผ่าน, ntfy topic, บัญชีที่เฝ้า, คุกกี้
- `subscriptions.json` — รายการ subscription (ครั้งแรกจะสร้างข้อมูลตัวอย่างให้ มีป้าย "ตัวอย่าง")
- `likes.json` — Google client id/secret, token, คลิปที่ไลค์และสถานะ (รอโพสต์/โพสต์แล้ว/ข้าม)
- `recordings/` — ไลฟ์ที่อัดไว้

## การอัปเดต
push ขึ้น `main` → เซิร์ฟเวอร์เช็ก GitHub ทุก 5 นาที แล้วรีสตาร์ตเองตอนไม่มีอะไรอัดอยู่ (ปกติขึ้นภายใน ~5 นาที)
ไม่ต้องรีบูต · อยากได้ทันทีจริง ๆ: `sudo liverec update`

## Subscription tracker
- ฟิลด์: ชื่อ, ราคา, THB/USD, รอบจ่าย (รายเดือน/รายปี/จ่ายตามใช้/ฟรี), วันต่ออายุถัดไป, หมวด, สถานะ (ใช้อยู่/จะยกเลิก/ยกเลิกแล้ว), โน้ต
- ยอดรวมนับเฉพาะรายเดือน/รายปีที่ยังไม่ยกเลิก · แปลง USD ด้วยเรตที่ตั้งเองในหน้า /subs
- เตือนก่อนต่ออายุ N วัน (ตั้งได้, ค่าเริ่ม 3) · สถานะ "จะยกเลิก" ได้ข้อความเตือนให้ไปยกเลิก
- เลยวันต่ออายุแล้ว ระบบเลื่อนไปรอบถัดไปให้เอง (เฉพาะ "ใช้อยู่")

## สิ่งที่จะทำต่อ
1. ใส่ราคา/วันต่ออายุจริงใน /subs (เจ้าของทำเองในหน้าเว็บ)
2. ตั้งค่า `/likes` ครั้งแรก (เจ้าของทำเอง ตามขั้นตอนในหน้า /likes): สร้าง Google Cloud project → เปิด YouTube Data API v3
   → OAuth consent External + **Publish (In production)** ไม่งั้น token หมดทุก 7 วัน → client แบบ "TVs and Limited Input devices"
   → วาง Client ID/secret → กด "เชื่อม YouTube" → ใส่รหัสที่ google.com/device
   พอใช้ได้แล้ว ยกเลิก IFTTT (฿2,100/ปี) และแก้สถานะใน /subs
3. หน้าคิวร้านเช่ากล้อง — แยกออกไปอยู่ข้างนอก (Cloudflare Pages `ชื่อร้าน.pages.dev`) ห้ามใช้โดเมนส่วนตัว
4. (ไม่ด่วน) ขยายอัด IG / ระบบสำรองไฟล์ไลฟ์

## ดีไซน์
คลีนแบบ spongelotus.work แต่ contrast สูง: พื้นหน้า #eef0f3 การ์ดขาว ตัวอักษร #0f1419 / #2b333c สีรอง #56606b
ฟอนต์ Geist (อังกฤษ) + Anuphan (ไทย), ปุ่มทรงแคปซูล, หัวข้อเล็ก
แต่ละส่วนมีสีของตัวเอง: อัดไลฟ์ แดง #ff4f45 · Subscriptions น้ำเงิน #2f7cf6 · Likes ม่วง #8e4ff0
ใช้กับแถบหัวหน้า, เมนูที่เลือก, ปุ่ม, หัวการ์ดใน Hub, ตัวเลขใหญ่
CSS อยู่ใน `recorder/web.py` (ตัวแปร `CSS`)

## ข้อตกลง
- ห้ามทำให้ recorder พัง ทดสอบก่อน push
- ข้อมูลส่วนตัวห้ามอยู่ในโค้ด
- หน้าเว็บภาษาไทย ใช้บนมือถือสะดวก
- โดเมน spongelotus.work ใช้กับ gallery เท่านั้น ห้ามแตะ
