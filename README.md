# Live Recorder (TikTok · IG ตามมาทีหลัง)

เฝ้าบัญชี TikTok แล้วอัดไลฟ์อัตโนมัติ 24 ชม. บนเซิร์ฟเวอร์ (Oracle Cloud Always Free)

- อัดได้ทั้งไลฟ์ปกติและโหมดเกม/LIVE Studio — ถ้าสลับโหมดกลางไลฟ์หรือเน็ตหลุด จะต่อใหม่ทันที
- เลือกความละเอียดสูงสุด (origin) · วิดีโอไม่มีคอมเมนต์/ของขวัญบนจอ
- จบไลฟ์แล้วรวมเป็น .mp4 (ถ้าสลับแนวตั้ง↔แนวนอน จะแยกเป็น part1, part2)
- แจ้งเตือนผ่านแอป ntfy ตอนเริ่มอัดและอัดเสร็จ
- หน้าเว็บมีรหัสผ่าน: ดูสถานะ, ดู/โหลด/ลบไลฟ์ย้อนหลัง, เพิ่ม/ลบบัญชีที่เฝ้า

## ติดตั้ง (บนเครื่อง Ubuntu)

```bash
curl -fsSL https://raw.githubusercontent.com/spongelotus/live-recorder/main/install.sh | sudo bash
```

จบแล้วจะแสดง ลิงก์หน้าเว็บ · รหัสผ่าน · ชื่อ ntfy topic

Oracle Cloud: ต้องเปิด Ingress พอร์ต 80 และ 443 ใน Security List ของ VCN ด้วย

## คำสั่งช่วย

| คำสั่ง | ทำอะไร |
|---|---|
| `sudo liverec info` | ดูลิงก์ รหัสผ่าน topic อีกครั้ง |
| `sudo liverec log` | ดูบันทึกการทำงานล่าสุด |
| `sudo liverec restart` | รีสตาร์ตระบบ |
| `sudo liverec update` | ดึงโค้ดใหม่ทันที (ปกติเช็กเองทุก 3 ชม.) |
| `sudo liverec newpassword` | สุ่มรหัสผ่านหน้าเว็บใหม่ |

## ที่เก็บไฟล์

- โค้ด: `/opt/live-recorder`
- ตั้งค่า: `/var/lib/live-recorder/settings.json`
- ไลฟ์: `/var/lib/live-recorder/recordings/<ชื่อบัญชี>/<วันเวลา>/`
