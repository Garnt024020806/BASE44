# GARNT TOOL REJOIN — คู่มือติดตั้งและใช้งาน (ภาษาไทย)

เครื่องมือ auto-rejoin สำหรับ Roblox ที่รันบน **UG Phone (Android) + Termux**
รองรับหลาย clone (`com.roblox.*`) ทำงานได้ทั้งแบบ **root** และ **non-root**
ใช้กับบัญชีและเครื่องของคุณเองเท่านั้น

> ⚠️ การใช้ตัวรันสคริปต์ (executor) ร่วมกับ heartbeat อาจขัดกับข้อกำหนดการใช้งานของ Roblox และเสี่ยงถูกแบน — ใช้ด้วยความรับผิดชอบของคุณเอง

---

## 1. สิ่งที่ต้องมี

| รายการ | รายละเอียด |
|---|---|
| อุปกรณ์ | UG Phone / Android 8 ขึ้นไป |
| Termux | ติดตั้งจาก F-Droid หรือ GitHub (ห้ามใช้เวอร์ชัน Play Store) |
| Python | 3.9 ขึ้นไป (ติดตั้งโดย `setup.sh`) |
| สิทธิ์ | พื้นที่เก็บข้อมูล (storage) + ปิดประหยัดแบตเตอรี่; ถ้าเป็น root ให้อนุญาต `su` |
| แพ็กเกจ | ติดตั้งแล้วอย่างน้อย 1 ตัว เช่น `com.roblox.client`, `com.roblox.clienu`, `com.roblox.clienv` |

---

## 2. ติดตั้งทีละขั้น

### ขั้นที่ 1 — เปลี่ยน mirror ของ Termux
```bash
termux-change-repo
```
เลือก mirror ไทย/สิงคโปร์ (เช่น `mirror1.kku.ac.th`) แล้วกด OK
ถ้าไม่ทำขั้นนี้ `pkg update` มักค้างหรือช้ามาก

### ขั้นที่ 2 — ติดตั้งด้วยคำสั่งเดียว
```bash
curl -Ls https://raw.githubusercontent.com/garnt/garnt-tool-rejoin/main/setup.sh | bash
```
คำสั่งนี้จะ: ขอสิทธิ์ storage → อัปเดตแพ็กเกจ → ติดตั้ง python/pip/tsu/libexpat/openssl
→ ติดตั้ง `rich` และ `requests` → ดาวน์โหลด `rejoin.py` ไปที่ `/sdcard/Download/rejoin.py`
และ `$HOME/rejoin.py` → เตรียมโฟลเดอร์ `RejoinTool/heartbeat/`

### ขั้นที่ 3 — ให้สิทธิ์ต่าง ๆ

1. **สิทธิ์พื้นที่เก็บข้อมูล** — ใน Termux พิมพ์ `termux-setup-storage` แล้วกด "Allow"
   ตรวจสอบด้วย `ls /sdcard/Download` ต้องเห็นไฟล์ได้
2. **ปิดการประหยัดแบตเตอรี่** — ตั้งค่า → แอป → UG Phone / Termux → แบตเตอรี่ → "ไม่จำกัด"
   (หรือ `dumpsys deviceidle whitelist +com.termux`)
3. **สิทธิ์ root** — พิมพ์ `su -c id` ต้องได้ `uid=0(root)`
   ถ้าขึ้น `Permission denied` ให้เปิดสิทธิ์ root ในแอปจัดการสิทธิ์ของ UG Phone
4. **Overlay (เฉพาะ non-root)** — ตั้งค่า → แอป → Termux → "แสดงทับแอปอื่น" เปิดไว้
   ช่วยให้ Termux ไม่ถูกระบบฆ่าเวลาสลับแอป
5. **Wake-lock** — ตัวเครื่องมือเรียก `termux-wake-lock` ให้เอง; ตรวจว่ามี notification ค้างอยู่

### ขั้นที่ 4 — รันโปรแกรม

**แบบ root (แนะนำ)**
```bash
su -c "export PATH=$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && cd /storage/emulated/0/Download && python ./rejoin.py"
```

**แบบ non-root**
```bash
cd ~ && python rejoin.py
```
หรือรันจาก `/sdcard/Download` ก็ได้ เพราะโปรแกรมใช้พาธแบบ absolute ไม่ขึ้นกับ cwd

---

## 3. เมนูการใช้งาน

```
  ██████╗  █████╗ ██████╗ ███╗   ██╗████████╗
 ██╔════╝ ██╔══██╗██╔══██╗████╗  ██║╚══██╔══╝
 ██║  ███╗███████║██████╔╝██╔██╗ ██║   ██║
 ██║   ██║██╔══██║██╔══██╗██║╚██╗██║   ██║
 ╚██████╔╝██║  ██║██║  ██║██║ ╚████║   ██║
  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝
           ควายเผือก rejoin
```

| เมนู | ใช้ทำอะไร |
|---|---|
| 1. เริ่ม auto-rejoin | เปิดหน้าจอสถานะสด (ตาราง #, แพ็กเกจ, บัญชี, สถานะ, จำนวนพลาด, จำนวน rejoin, ตรวจครั้งถัดไป, รายละเอียด) + เหตุการณ์ล่าสุด + uptime/แรมว่าง — กด `Ctrl+C` เพื่อหยุดและกลับเมนู |
| 2. แสดงอินสแตนซ์ / สถานะ | ดูรายการที่ตรวจพบและสถานะล่าสุดโดยไม่เริ่มทำงาน |
| 3. ตั้งค่า | ลิงก์เกม, ระยะเวลาตรวจ (30 วิ), ดีเลย์ก่อนเปิดแอป (15 วิ) |
| 4. สแกนแพ็กเกจ | ค้นหา `com.roblox.*` ทั้งหมด แล้วเลือกว่าจะรันตัวไหน เช่น `1,3` หรือ `all` |
| 5. ล็อกอินคุกกี้ | อ่าน `cookie.txt`, ตรวจสอบชื่อบัญชี, และ (root) เขียนคุกกี้ลง clone |
| 6. ตั้งค่าอัตโนมัติ | เปิด/ปิดพฤติกรรมอัตโนมัติ + เมนูย่อย Heartbeat และ Performance |
| 7. Discord webhook | แจ้งเตือนเข้า Discord พร้อมเลือกเหตุการณ์ |
| 0. ออก | ปิดโปรแกรม (ปลด wake-lock ให้) |

### ลิงก์เกมที่รองรับ
- Place ID ตรง ๆ เช่น `1818`
- `https://www.roblox.com/games/1818/Classic-Crossroads`
- ลิงก์ private server ที่มี `privateServerLinkCode` → แปลงเป็น `roblox://placeID=1818&linkCode=XXXX`
- ลิงก์แชร์ `https://www.roblox.com/share?code=...`
- `roblox://` ตรง ๆ

เปิดแอปด้วย `am start -a VIEW -d <uri> -p <package>` และถ้าล้มเหลวจะ fallback ไป `monkey`

---

## 4. การทำงานหลัก

- **ทุกอินสแตนซ์ทำงานขนานกัน** แต่มี "ประตูเปิดแอป" ส่วนกลาง: ห่างกันอย่างน้อย `launch_delay` (ค่าเริ่มต้น 15 วิ) ทุกครั้ง ทั้งตอนเปิดรอบแรกและตอน rejoin
- **ตรวจทุก 30 วิ** โดยทุกอินสแตนซ์ถูกตรวจบนตารางเวลาเดียวกัน (time grid) — ลดภาระ CPU
- **ถอยหลังแบบทวีคูณ (backoff)** เมื่อพลาดซ้ำ: 10 → 20 → 40 … สูงสุด 300 วิ และรีเซ็ตเมื่อเสถียรต่อเนื่อง 5 นาที
- **ช่วงผ่อนผันหลังเปิดแอป (join grace)** 90 วิ: ในช่วงนี้ "ยังไม่อยู่ในเกม" ยังไม่นับว่าพลาด
- **root**: ตรวจ process, ตรวจ foreground (ปิดไว้เป็นค่าเริ่มต้น เพราะหลาย clone อยู่ foreground พร้อมกันไม่ได้), force-stop ก่อนเปิดใหม่, screencap, ใส่คุกกี้, ล้างแรม
- **non-root**: สถานะมาจาก Roblox presence API (เมื่อมีคุกกี้) ถ้าไม่มีจะแสดง "ไม่ทราบ" และเปิดตัวเลือก rejoin ตามเวลา (นาที)

---

## 5. คุกกี้ (cookie.txt)

วางไฟล์ที่ `/sdcard/Download/RejoinTool/cookie.txt` — หนึ่งคุกกี้ต่อบรรทัด
จับคู่กับอินสแตนซ์ที่ตรวจพบ **ตามลำดับ** (บรรทัดว่างและบรรทัดที่ขึ้นต้นด้วย `#` จะถูกข้าม)

```
# บัญชีที่ 1
.ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS...
# บัญชีที่ 2
"_|WARNING:-DO-NOT-SHARE-THIS..."
```

- ระบบตรวจคุกกี้ผ่าน API `users/v1/users/authenticated` แล้วแสดงชื่อผู้ใช้
- **root**: เขียน `.ROBLOSECURITY` ลงฐานข้อมูล WebView Cookies ของแต่ละ clone (หาไฟล์ → force-stop → copy ออก → แก้ตาม schema จริงของตาราง → copy กลับ + chown/chmod + restorecon)
- **non-root**: ตรวจสอบเท่านั้น ไม่สามารถเขียนได้
- คุกกี้ถูก **มาสก์ในทุกจุดที่แสดงผล** ไม่ถูกเขียนลง log, ไม่ถูกส่งเข้า webhook, ไม่ถูกเก็บใน config.json

---

## 6. Heartbeat (สคริปต์ในเกม)

ตัวเครื่องมือเปิด HTTP receiver เฉพาะ localhost ที่ `127.0.0.1:8765`

- `GET /hb?id=<package>&user=<name>&uid=<id>&place=<placeid>&job=<jobid>`
- `GET /ping` (ทดสอบว่า receiver ทำงาน)

สคริปต์ Lua (สร้างจากเมนู 6 → Heartbeat → "สร้างไฟล์ .lua") จะส่ง ping ทุก 15 วิ
**เฉพาะเมื่อ** Heartbeat ของเกมเดินหน้าภายใน 5 วิ และไม่มีหน้าต่าง error/disconnect ค้างอยู่
รองรับ `request` / `syn.request` / `http_request` และ fallback ไป `game:HttpGet`
พร้อมกันรันซ้ำด้วย `_G` guard

**กติกาของ engine**
- อินสแตนซ์ที่เคยส่ง heartbeat แล้วเงียบเกิน `timeout` (90 วิ) = พลาด
- หลังเปิดแอปใหม่ ถ้าเลยช่วงผ่อนผันแล้วยังไม่เคยส่ง = พลาด
- ถ้า **ไม่เคย** ส่งเลย จะไม่ใช้กติกานี้ (กัน rejoin วนไม่จบ) และกลับไปใช้การตรวจอื่น

---

## 7. Performance

- **ล้างแรม (root)**: `drop_caches` ทุก 30 นาที และ/หรือเมื่อ `MemAvailable < 300 MB` (ห่างกันอย่างน้อย 1 นาที) — เลือก `am kill-all` ได้ (ปิดไว้ เพราะอาจฆ่า clone ที่อยู่เบื้องหลัง) — มีปุ่ม "ล้างเดี๋ยวนี้" และบันทึกหน่วยความจำก่อน/หลัง
- **ลด CPU**: snapshot คำสั่ง shell 1 ครั้งต่อรอบตรวจ (process list + foreground ถ้าเปิด) ใช้ร่วมกันทุกอินสแตนซ์ด้วย cache แบบมี lock + TTL, ตรวจบน time grid เดียวกัน, `os.nice(10)`, cache ผล presence 60 วิ, รีเฟรชหน้าจอสดทุก 2 วิ (ปรับได้)

---

## 8. Discord webhook

- เปิด/ปิดหลัก + เลือกเหตุการณ์: เริ่ม, crash, rejoin, หยุด และ "status"
- ตรวจรูปแบบ URL, ส่งข้อความทดสอบได้, แนบภาพหน้าจอ (root เท่านั้น)
- เก็บภาพหน้าจอ 20 ไฟล์ล่าสุดในโฟลเดอร์ `screenshots/`
- รายงานสถานะเป็นระยะ + ภาพหน้าจอเต็มจอทุก N นาที (`webhook.periodic_min`, ค่าเริ่มต้น 10, `0` = ปิด) — non-root ส่งข้อความอย่างเดียว
- จัดการ `429` ของ Discord (รอตาม `retry_after`) และส่งเหตุการณ์ "หยุด" แบบ synchronous

---

## 9. ไฟล์และ log

```
/sdcard/Download/RejoinTool/
├── config.json          # ตั้งค่าทั้งหมด (บันทึกแบบ atomic)
├── cookie.txt           # คุกกี้ (ไม่ถูกอ่านเข้า config)
├── logs/                # หมุนไฟล์ทุกเที่ยงคืน เก็บ 7 วัน
├── screenshots/         # 20 ไฟล์ล่าสุด
└── heartbeat/           # ไฟล์ .lua ที่สร้างให้แต่ละอินสแตนซ์
```
ถ้าเขียน `/sdcard` ไม่ได้ จะ fallback ไปที่ `$HOME/RejoinTool/`
ไฟล์ config เสียหาย → สำรองเป็น `config.json.corrupt-<เวลา>` แล้วสร้างค่าเริ่มต้นใหม่

---

## 10. แก้ปัญหาที่พบบ่อย

| อาการ | สาเหตุ | วิธีแก้ |
|---|---|---|
| `ModuleNotFoundError: rich` | ยังไม่ได้ติดตั้งไลบรารี | `pip install rich requests` หรือรัน `setup.sh` ใหม่ |
| `CANNOT LINK EXECUTABLE` | `LD_LIBRARY_PATH` เพี้ยนจากแอปอื่น | `unset LD_LIBRARY_PATH` แล้วรันใหม่ หรือ `export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib` |
| `su: not granted` / `Permission denied` | ยังไม่อนุญาต root | เปิดแอปจัดการสิทธิ์ root ของ UG Phone แล้วอนุญาต Termux, ทดสอบ `su -c id` |
| อ่าน/เขียน `/sdcard` ไม่ได้ | ไม่ได้ให้สิทธิ์ storage | `termux-setup-storage` → Allow → `ls /sdcard/Download` |
| สแกนไม่พบแพ็กเกจ | ยังไม่ได้ติดตั้ง หรือชื่อไม่ขึ้นต้น `com.roblox.` | ติดตั้ง clone ก่อน แล้วเมนู 4 อีกครั้ง หรือพิมพ์ชื่อแพ็กเกจเอง |
| deep link ไม่เปิดแอป | แอปไม่รับ intent หรือ URI ผิดรูปแบบ | ตรวจ Place ID, เปิด "fallback monkey" (ทำงานอัตโนมัติ) และดู log |
| heartbeat ไม่เข้า | receiver ไม่ทำงาน / พอร์ตชน / สคริปต์ไม่ได้รัน | เปิดเบราว์เซอร์ในเครื่องไปที่ `http://127.0.0.1:8765/ping`, เปลี่ยนพอร์ตในเมนู 6 |
| webhook ไม่ส่ง | URL ผิด หรือถูกจำกัด rate limit | ใช้ปุ่ม "ทดสอบ" ในเมนู 7, ตรวจ 429 ใน log |
| สถานะขึ้น "ไม่ทราบ" ตลอด | non-root และไม่มีคุกกี้ | เพิ่ม `cookie.txt` หรือเปิด timed rejoin ในเมนู 6 |
| หน้าจอสดกระพริบ/หนัก | เครื่องช้า | เพิ่ม `ui.refresh_sec` เป็น 3–5 ในเมนู 6 → Performance |

---

## 11. ข้อสมมติ

- `REPO_RAW_BASE` ใน `setup.sh` คือ `https://raw.githubusercontent.com/garnt/garnt-tool-rejoin/main` — แก้เป็น repo ของคุณก่อนเผยแพร่
- ค่าเริ่มต้น: ตรวจทุก 30 วิ, ดีเลย์เปิดแอป 15 วิ, join grace 90 วิ, heartbeat 8765/15 วิ/timeout 90 วิ, รายงานสถานะทุก 10 นาที, เก็บ log 7 วัน
- การเขียนคุกกี้ลง clone ต้องมี root และโครงสร้าง WebView ของ clone นั้น ๆ — ถ้าโครงสร้างต่างจากปกติ ระบบจะรายงาน error ใน log โดยไม่ทำให้ไฟล์เสียหาย
