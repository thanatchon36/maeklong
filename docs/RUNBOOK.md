# RUNBOOK — riverbank (คู่มือปฏิบัติงานสำหรับผู้ดูแลระบบ)

> เอกสารนี้ครอบคลุม shadow / demo / M1 validation (§1–§9) และโหมด live สำหรับ M5–M7 (§10)
> **ห้ามรันโหมด `live` จนกว่า acceptance ของ M1, M3, M4 จะผ่านและ operator อนุมัติ** (SPEC §0.1, §11, OPEN_QUESTIONS B1)
> โค้ดบล็อก live ไว้ด้วย `LIVE_ACCEPTANCE_APPROVED = False` และ compose ไม่มี service สำหรับ live โดยเจตนา
> แผนการทดสอบรับมอบ (acceptance) แบบละเอียดอยู่ที่ `docs/ACCEPTANCE_PLAN.md`

ศัพท์ที่ใช้บ่อย (คงไว้เป็นภาษาอังกฤษเพราะตรงกับ log และโค้ด):
`HALTED` = หยุดทุกอย่างและรอคนสั่งรีสตาร์ต, `REDUCE_ONLY` = ส่งได้เฉพาะคำสั่งที่ลด position,
`RECON_MISMATCH` = สถานะในเครื่องไม่ตรงกับตลาด, dead-man = ตัวนับถอยหลังฝั่ง Binance ที่ยกเลิกคำสั่งให้เองถ้าบอทหายไป

---

## 1. ภาพรวม

| โหมด | ส่งคำสั่งจริงไหม | ใช้คีย์ไหม | ไฟล์ config | state / DB | kill file | metrics |
|---|---|---|---|---|---|---|
| `shadow` | ไม่ (จำลองการ fill) | ไม่ใช้ | `config/config.yaml` | `state/riverbank.sqlite` | `./KILL` | `:9108` |
| `demo` | ใช่ แต่เป็นเงินปลอม (`demo-fapi.binance.com`) | คีย์ **demo** | `config/demo.yaml` | `state/demo/riverbank.sqlite` | `./KILL.demo` | `:9109` |
| M1 validation | ไม่ | ไม่ใช้ | — | — | — | — |
| `live` (§10) | **ใช่ เงินจริง** | คีย์ **production** | `config/live.yaml` | `state/live/riverbank.sqlite` | `./KILL` (ใช้ร่วมกับ shadow) | `:9110` |

- shadow และ demo รันพร้อมกันได้ เพราะใช้ state, kill file, พอร์ต metrics และบอท Telegram แยกกัน
- live **ต้อง**รันคู่กับ shadow เพราะเลือก symbol จากผลของ shadow (อ่าน `state/riverbank.sqlite` แบบอ่านอย่างเดียว)
- ข้อมูลตลาดดิบอยู่ที่ `data/` (shadow), `data/demo/` (demo) และ `data/m1-validation/` (M1)
- รายงานรายวันจะสร้างให้อัตโนมัติหลังเที่ยงคืน UTC ที่ `reports/YYYY-MM-DD/` (shadow) และ `reports/demo/YYYY-MM-DD/` (demo)
- การแจ้งเตือนทุกรายการถูกเขียนลง `logs/alerts.log` ด้วยเสมอ แม้ Telegram จะใช้งานไม่ได้

---

## 2. เตรียมเครื่อง (ครั้งแรกครั้งเดียว)

แนะนำ VPS Linux ที่มี IP คงที่ ใกล้ Tokyo, 2 vCPU / 4 GB RAM / ดิสก์อย่างน้อย 40 GB

```bash
# 1) แพ็กเกจพื้นฐาน
sudo apt-get update && sudo apt-get install -y git tmux sqlite3 chrony
timedatectl            # ต้องเห็น "System clock synchronized: yes" ถ้าเวลาเพี้ยน คำขอที่เซ็นชื่อจะล้มด้วย -1021
# 2) Docker (ตามคู่มือทางการของ Docker สำหรับ distro นั้น) และ uv
curl -LsSf https://astral.sh/uv/install.sh | sh
# 3) โค้ด
git clone <repo> ~/maeklong && cd ~/maeklong
uv sync && uv run pytest -q          # ต้องผ่านทั้งหมด
# 4) ค่าสำหรับ docker compose
cp docker/env.example docker/.env
# แก้ docker/.env: RB_UID=$(id -u), RB_GID=$(id -g), GRAFANA_ADMIN_PASSWORD, Telegram ของ shadow, DEMO_ENV_FILE
```

### 2.1 ความลับ (secrets)

- **ห้าม**ใส่คีย์ใน `config/*.yaml`, `docker/.env` หรือ commit เข้า git เด็ดขาด
- คีย์ demo ให้เก็บในไฟล์นอก repo แล้วกำหนด `DEMO_ENV_FILE` ใน `docker/.env` ให้ชี้มาที่ไฟล์นี้

```bash
mkdir -p ~/.riverbank && chmod 700 ~/.riverbank
cat > ~/.riverbank/demo.env <<'EOF'
BINANCE_API_KEY=<demo key>
BINANCE_API_SECRET=<demo secret>
TELEGRAM_BOT_TOKEN=<token ของบอท Telegram ตัวที่ 2>
TELEGRAM_CHAT_ID=<chat id>
EOF
chmod 600 ~/.riverbank/demo.env
```

- บัญชี demo ต้องอยู่ในโหมด **Single-Asset** (ถ้าเป็น Multi-Assets บอทจะไม่ยอมเริ่มทำงาน) และต้องไม่มี position หรือคำสั่งค้างที่เปิดเอง
- **Telegram ต้องใช้บอท 2 ตัว** ตัวหนึ่งสำหรับ shadow อีกตัวสำหรับ demo
  เพราะถ้าสอง process ดึงข้อความจาก token เดียวกันจะชนกัน (HTTP 409) และคำสั่ง `/kill` จะไปหยุดทั้งสองโหมดพร้อมกัน
  - สร้างบอทด้วย @BotFather → `/newbot`
  - ส่งข้อความหาบอทหนึ่งครั้ง แล้วดู `chat.id` จาก `curl https://api.telegram.org/bot<TOKEN>/getUpdates`

---

## 3. เริ่ม / หยุด / ดูสถานะ

ให้รันทุกคำสั่งจาก root ของ repo จะสะดวกกว่า ถ้าตั้ง alias ไว้ดังนี้:

```bash
alias dc='docker compose -f docker/docker-compose.yml'
```

| งาน | คำสั่ง |
|---|---|
| เริ่ม shadow + Prometheus + Grafana | `dc up -d --build` |
| เริ่ม demo | `dc --profile demo up -d demo` |
| เริ่ม M1 validation (25 ชม.) | `dc --profile m1 up -d m1` |
| ดูสถานะ / health | `dc ps` |
| ดู log สด | `dc logs -f --tail 100 shadow` (หรือ `demo`, `m1`) |
| หยุดแบบปกติ (ยกเลิกคำสั่ง `rb-*` ทั้งหมดและปิด dead-man ก่อนออก) | `dc stop demo` (รอได้สูงสุด 90 วินาที) |
| อัปเดตโค้ดแล้วเริ่มใหม่ | `git pull && dc up -d --build shadow` |

- บอทจะรีสตาร์ตตัวเองเมื่อ process ตาย (`restart: unless-stopped`)
- แต่ถ้ามีสถานะ HALT บันทึกไว้ หรือมีไฟล์ kill อยู่ บอทจะ**ปฏิเสธการเริ่มทำงานทุกครั้ง** โดยพิมพ์ `REFUSED: refusing to start: ...` แล้วออก
  docker จะพยายามรีสตาร์ตซ้ำเรื่อย ๆ ซึ่งไม่เป็นอันตราย การวนแบบนี้เป็นสัญญาณให้คนเข้าไปตรวจสอบ (ดู §5)

### 3.1 รันโดยไม่ใช้ Docker

```bash
tools/run_shadow.sh                                         # shadow (สคริปต์จะ unset คีย์ Binance ทั้งหมดให้)
set -a; source ~/.riverbank/demo.env; set +a; tools/run_demo.sh  # demo
HOURS=25 SYMBOLS=20 tools/run_m1_validation.sh              # M1
```

ควรรันใน `tmux` เพื่อไม่ให้ process หลุดเมื่อ SSH ขาด

### 3.2 หมายเหตุสำหรับ macOS (Docker Desktop)

- **ห้ามเปิดไฟล์ `state/*.sqlite` จากฝั่ง host** (ทั้ง `sqlite3`, `kill_drill.py`, `demo_acceptance.py` ฯลฯ) ระหว่างที่บอทในคอนเทนเนอร์ยังรันอยู่
  เพราะ file lock ของ SQLite ใช้ข้าม VM ของ Docker Desktop ไม่ได้
  ตอนทดสอบเมื่อ 2026-10-12 การอ่าน DB จาก host เพียงครั้งเดียวทำให้ DB **เสียหายจนใช้ไม่ได้** (header กลายเป็นศูนย์ทั้งหมด)
- บน macOS ให้เลือกอย่างใดอย่างหนึ่ง:
  - รันบอทด้วย `uv` ตรง ๆ (§3.1)
  - หรือรันเครื่องมือทุกตัวในคอนเทนเนอร์ผ่าน `dc exec demo python tools/...` (ในคอนเทนเนอร์ไม่มีโปรแกรม `sqlite3` ให้ใช้ `python -c "import sqlite3; ..."` แทน)
- บน Linux (VPS) ใช้งาน bind mount ได้ตามปกติ เพราะ host กับคอนเทนเนอร์ใช้ kernel เดียวกัน
- ถ้าใช้แล็ปท็อปรันงานยาว ต้องสั่ง `caffeinate -dimsu` ไว้ด้วย เพื่อไม่ให้เครื่องหลับ

---

## 4. Kill switch (หยุดฉุกเฉิน: ต้องเข้า HALTED ภายใน 1 วินาที)

เลือกวิธีใดก็ได้ ทุกวิธีให้ผลเหมือนกัน คือยกเลิกคำสั่งทั้งหมด ปิด position ด้วย reduce-only limit แล้วหยุดถาวร

| วิธี | shadow | demo | live (§10) |
|---|---|---|---|
| ไฟล์ | `touch KILL` | `touch KILL.demo` | `touch KILL` (**หยุดทั้ง shadow และ live**) |
| สัญญาณ | `dc kill -s SIGUSR1 shadow` หรือ `kill -USR1 <pid>` | `dc kill -s SIGUSR1 demo` | `kill -USR1 <pid ของ live>` |
| Telegram | ส่ง `/kill` ไปที่บอท #1 | ส่ง `/kill` ไปที่บอท #2 | ส่ง `/kill` ไปที่บอท #3 |

- คำสั่ง Telegram `/status` จะตอบสถานะปัจจุบันกลับมา
- Telegram รับคำสั่งเฉพาะจาก `TELEGRAM_CHAT_ID` ที่ตั้งไว้เท่านั้น

**ถ้าบอทค้างหรือไม่ตอบสนอง** ให้ใช้เครื่องมือ standalone ซึ่งทำงานได้โดยไม่ต้องมีบอทรันอยู่:

```bash
set -a; source ~/.riverbank/demo.env; set +a
uv run python tools/cancel_all.py                 # dry run: แสดงรายการคำสั่ง rb-* ที่ยังค้างบน demo
uv run python tools/cancel_all.py --execute       # ยกเลิกจริง
uv run python tools/flatten_all.py                # dry run: แสดง position
uv run python tools/flatten_all.py --execute      # ปิด position ด้วย reduce-only limit (เพิ่ม --taker ได้เฉพาะกรณีฉุกเฉินจริง)
```

ถ้า process ตายไปเลย dead-man ฝั่ง Binance จะยกเลิกคำสั่งของเราให้เองภายในราว 30 วินาที
ให้ตรวจยืนยันด้วย `cancel_all.py` แบบ dry run

---

## 5. กู้คืนจาก HALTED

1. **ห้ามรีสตาร์ตทันที** ให้หาสาเหตุก่อนเสมอ:
   ```bash
   sqlite3 state/demo/riverbank.sqlite \
     "select datetime(ts/1000,'unixepoch'),level,rule,detail,state_after from risk_events order by ts desc limit 20"
   tail -50 logs/alerts.log
   dc logs --since 1h demo | grep -E '"level":"(error|critical)"' | tail -50
   ```
2. ยืนยันว่าบนตลาดไม่มีคำสั่งหรือ position ค้าง (`cancel_all.py` / `flatten_all.py` แบบ dry run)
3. บันทึกสาเหตุลง `logs/fault-log.md` (วันเวลา UTC, อาการ, สาเหตุ, สิ่งที่แก้)
4. ล้างสถานะ HALT แล้วเริ่มใหม่:
   ```bash
   uv run python tools/clear_halt.py --db state/demo/riverbank.sqlite --kill-file KILL.demo   # จะถามยืนยัน
   # shadow: uv run python tools/clear_halt.py   (ค่า default คือ state/riverbank.sqlite และ KILL)
   dc restart demo
   ```

**ระดับความรุนแรง** (OPEN_QUESTIONS B5):

| สาเหตุ | ผล |
|---|---|
| position / gross / beta / leverage เกินเพดาน, liquidation ใกล้กว่าที่กำหนด, ขาดทุนรายวันเกิน 75% ของเพดาน | `REDUCE_ONLY` (กลับเป็น OK เองเมื่อทุกตัวต่ำกว่า 90% ของเพดาน) |
| ขาดทุนรายวันถึงเพดาน, drawdown, kill switch, exception ในเส้นทาง order/risk, HTTP 429/418, user stream หลุดเกิน 60 วินาทีขณะมี position, recon แก้ไม่ได้ (3 ครั้งติด) | `HALTED` (ต้องให้คนสั่งเริ่มใหม่) |

---

## 6. นโยบายการเพิ่มขนาด (อ้างอิงจาก OPEN_QUESTIONS A1)

- โปรแกรม**ไม่เพิ่ม**ขนาดคำสั่งหรือเพดานความเสี่ยงเองเด็ดขาด (SPEC §11.5) ทุกการเปลี่ยนแปลงต้องให้ operator แก้ config เอง
- ใช้ได้หลังผ่าน M5 แล้วเท่านั้น และต้องเข้าเงื่อนไขทุกข้อ:
  - เพิ่มได้ไม่เกิน **×1.5 ต่อครั้ง**
  - เพิ่มได้ไม่เกิน **1 ครั้งต่อ 14 วัน**
  - symbol นั้นต้องมี live fills **≥ 500** โดย realized edge > 0 ที่ระดับ 1σ (`mean − 1·SE > 0`)
  - ในช่วงนั้นต้องไม่มี HALT และไม่มี `RECON_MISMATCH` ที่ค้างอยู่
- ให้บันทึกทุกครั้งที่เปลี่ยน (วันที่, ค่าเดิม → ค่าใหม่, หลักฐาน) ไว้ใน commit message ของ config

---

## 7. งานประจำวัน (ใช้เวลาประมาณ 5 นาที)

1. เปิด Grafana แล้วไล่ดูตามนี้:
   - risk state ต้องเป็น OK
   - bot ต้อง UP
   - recon mismatch 24 ชม. ต้องเป็น 0
   - rate-limit ต้องต่ำกว่า 60%
   - Book OK ต้องเป็นสีเขียวเกือบตลอด
2. `tail -20 logs/alerts.log` ดูว่ามีอะไรผิดปกติหรือไม่
3. เช็กว่ารายงานของเมื่อวานถูกสร้างแล้ว: `ls reports/$(date -u -d yesterday +%F)/`
4. เช็กพื้นที่ดิสก์: `df -h . && du -sh data` (ข้อมูลดิบเก็บไว้ 30 วันแล้วลบเอง)
5. เฉพาะ demo/live: ดูว่าไม่มีเตือน `apiTradingStatus` (ตัวชี้วัดของ Binance เกิน 70% ของเกณฑ์)
   ถ้าเกิน แปลว่ายกเลิกคำสั่งบ่อยเกินไป ให้พิจารณาเพิ่ม `quoting.requote_ticks`

### เปิดดู Grafana / Prometheus จากเครื่องตัวเอง

ทั้งสองตัวเปิดพอร์ตไว้เฉพาะ localhost ของ VPS ต้องเข้าผ่าน SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 <user>@<vps>
# แล้วเปิด http://localhost:3000  (user: admin, รหัสผ่านตาม GRAFANA_ADMIN_PASSWORD)
```

แดชบอร์ด `riverbank` ถูกตั้งเป็นหน้าแรก ใช้ตัวเลือก `bot` ด้านบนสลับระหว่าง shadow และ demo ได้

---

## 8. เหตุการณ์ที่พบบ่อยและวิธีรับมือ

| อาการ | สาเหตุที่เป็นไปได้ | ทำอย่างไร |
|---|---|---|
| คอนเทนเนอร์รีสตาร์ตวนพร้อม `REFUSED: refusing to start` | มี HALT บันทึกไว้ หรือมีไฟล์ `KILL` / `KILL.demo` | ทำตาม §5 |
| `REFUSED` เรื่อง Multi-Assets หรือ position mode (demo) | ตั้งค่าบัญชีไม่ถูกต้อง | เปลี่ยนบัญชีเป็น Single-Asset ปิด position ที่เปิดเอง แล้วเริ่มใหม่ |
| HALTED จาก 429 / 418 | ส่งคำขอเกิน rate limit | **ห้ามรีสตาร์ตทันที** ต้องรออย่างน้อยเท่ากับ `Retry-After` (ขั้นต่ำ 60 วินาที) การโดน 418 ซ้ำจะทำให้ IP ถูกแบนนานขึ้นเรื่อย ๆ (สูงสุด 3 วัน) ให้หาสาเหตุใน log ก่อน |
| `RECON_MISMATCH` | สถานะในเครื่องไม่ตรงกับตลาด | บอทจะยกเลิกคำสั่งของ symbol นั้นและรับสถานะจากตลาดมาใช้เอง ถ้าเกิด 3 ครั้งติดจะ HALT ให้ดู `recon_events` ใน DB |
| แจ้งเตือน disconnect เกิน 30 วินาที | เครือข่ายมีปัญหา | ระหว่างนั้นบอทจะดึงคำสั่งออก เมื่อเชื่อมต่อกลับได้จะ resync และ reconcile ก่อนกลับมา quote ถ้าเกิดบ่อยให้ตรวจเครือข่ายของ VPS |
| Book OK = 0 ค้างที่ symbol เดียว | symbol นั้นมีสภาพคล่องต่ำ (STALE) หรือข้อมูลขาดช่วง | ถ้าเป็นแค่ชั่วครู่ถือว่าปกติ ถ้าค้างนานให้พิจารณาใส่ symbol นั้นใน `config/blacklist.yaml` |
| health เป็น `unhealthy` | พอร์ต metrics ไม่ตอบ | ดู `dc logs` ถ้า process ค้างให้ใช้ `dc restart` (ปลอดภัยเพราะ dead-man ฝั่ง Binance คุ้มครองคำสั่งอยู่) |
| ดิสก์ใกล้เต็ม | ข้อมูลดิบสะสมมาก | ลด `ops.raw_retention_days` หรือย้าย `data/` ไปดิสก์อื่น ห้ามลบ `state/` |

---

## 9. การทดสอบรับมอบ (M1 / M3 / M4)

ดูขั้นตอน คำสั่ง และเกณฑ์ผ่าน/ไม่ผ่านแบบละเอียดได้ที่ `docs/ACCEPTANCE_PLAN.md` สรุปคำสั่งเมื่อใช้ Docker:

```bash
dc --profile m1 up -d m1                                   # M1: รายงานที่ reports/m1-validation/report.md
dc up -d shadow                                            # M3: รัน 7 วันขึ้นไป แล้วสั่ง:
uv run python tools/shadow_acceptance.py --start D0 --end D6
dc --profile demo up -d demo                               # M4: รัน 72 ชม. + fault injection + kill drill แล้วสั่ง:
uv run python tools/demo_acceptance.py --db state/demo/riverbank.sqlite
```

---

## 10. โหมด live (M5–M7) — เงินจริง

> ทุกขั้นในหัวข้อนี้ใช้ได้ **หลังจาก** acceptance ของ M1, M3, M4 ผ่านและ operator อนุมัติแล้วเท่านั้น (OPEN_QUESTIONS B1)
> ถ้ายังไม่ผ่าน ห้ามข้ามขั้น 10.1

### 10.1 ปลดล็อก live (ทำครั้งเดียว และต้องมีหลักฐาน)

1. ตรวจว่ามีรายงานผ่านครบทั้ง 3 ฉบับ: `reports/m1-validation/report.md`, `reports/m3-acceptance/report.md`, `reports/m4-acceptance/report.md`
2. แก้ `src/riverbank/orders/live_venue.py` จาก `LIVE_ACCEPTANCE_APPROVED = False` เป็น `True`
3. commit โดยใส่ลิงก์หรือชื่อไฟล์รายงานทั้ง 3 ฉบับไว้ใน commit message และแก้ OPEN_QUESTIONS B1 ให้บันทึกวันที่อนุมัติ
4. `uv run pytest -q` ต้องผ่านทั้งหมด (test `test_live_selection_stays_blocked_until_acceptance_is_approved` จะ fail หลังปลดล็อก ให้แก้ test นั้นใน commit เดียวกันให้ตรวจว่า flag เป็น `True` ตามที่อนุมัติ)

แม้ปลดล็อกแล้ว บอทยังต้องผ่าน gate อื่นทุกข้อ: `MODE=live`, `LIVE_CONFIRM`, และ startup checks (§10.4)

### 10.2 เตรียมบัญชีและคีย์ production

- บัญชี Futures ต้องเป็น **Single-Asset** และ **one-way** และต้องไม่มี position หรือคำสั่งที่เปิดเอง
- ฝากเงินเท่าที่ยอมเสียได้ทั้งหมด เพดานความเสี่ยงใน `config/live.yaml` เป็นตัวเลข USDT คงที่ ไม่ปรับตามยอดเงิน
- สร้าง API key ใหม่สำหรับบอทนี้โดยเฉพาะ:
  - เปิดเฉพาะ **Enable Futures** (และ Reading)
  - **ห้ามเปิด Withdrawals** (บอทจะไม่ยอมเริ่มถ้าเปิด)
  - **ต้องจำกัด IP** ให้เป็น IP คงที่ของ VPS (บอทจะไม่ยอมเริ่มถ้าไม่จำกัด)
- เก็บคีย์ในไฟล์นอก repo และใช้บอท Telegram ตัวที่ 3 (แยกจาก shadow และ demo):

```bash
cat > ~/.riverbank/live.env <<'EOT'
BINANCE_API_KEY=<production key>
BINANCE_API_SECRET=<production secret>
TELEGRAM_BOT_TOKEN=<token ของบอท Telegram ตัวที่ 3>
TELEGRAM_CHAT_ID=<chat id>
EOT
chmod 600 ~/.riverbank/live.env
```

### 10.3 เลือก symbol ที่จะเทรด (allow-list)

live จะ quote เฉพาะ symbol ที่ผ่าน **ทุกข้อ** (OPEN_QUESTIONS B10, B11, B21):

- อยู่ใน universe ที่ screener คัดไว้ในขณะนั้น
- ใน shadow มี fill อย่างน้อย `min_fills_for_promotion` (300) และ realized edge ที่ 60 วินาทีผ่าน `mean − 1·SE > 0`
- อยู่ใน `universe.live_symbols` ใน `config/live.yaml` (เว้นแต่ตั้ง `auto_promote: true` ซึ่ง**ไม่แนะนำ**)
- ไม่ได้อยู่ในช่วง cooldown หลังถูก demote (24 ชม.) และหลังถูก demote ต้องมีผล shadow ชุดใหม่ที่เกิดหลังวันที่ demote
- รวมแล้วไม่เกิน `max_active_symbols` (3)

ดูว่า symbol ไหนพร้อม: เปิดหัวข้อ "Promotion readiness" ในรายงานรายวันของ shadow (`reports/YYYY-MM-DD/report.md`)
แล้วใส่ชื่อที่ต้องการลงใน allow-list เช่น `live_symbols: [DOGEUSDT, XRPUSDT]`
**ถ้า allow-list ว่าง live จะไม่ quote อะไรเลย**

### 10.4 เริ่ม live

live ยังไม่มี service ใน docker compose ให้รันด้วย `uv` ใน `tmux` บน VPS Linux (เครื่องเดียวกับ shadow)
**ห้ามรัน live บน macOS Docker Desktop** เพราะ live ต้องอ่าน DB ของ shadow ซึ่งข้าม VM ไม่ได้ (§3.2)

```bash
tmux new -s live
cd ~/maeklong
set -a; source ~/.riverbank/live.env; set +a
MODE=live LIVE_CONFIRM=I_UNDERSTAND_THE_RISK uv run python -m riverbank.main --config config/live.yaml
```

ตอนเริ่ม บอทจะตรวจตามลำดับและ**ไม่ยอมเริ่ม** (`REFUSED`) ถ้าข้อใดไม่ผ่าน:

| ตรวจ | ถ้าไม่ผ่าน |
|---|---|
| `LIVE_ACCEPTANCE_APPROVED` | ยังไม่ปลดล็อก → ทำ §10.1 |
| `MODE=live` และ `LIVE_CONFIRM` ถูกต้อง | ตั้ง env ให้ครบ |
| ไม่มี HALT ค้างใน `state/live/` และไม่มีไฟล์ `KILL` | ทำตาม §5 |
| พบ `universe.shadow_db` และไม่ใช่ DB ของ live เอง | ต้องมี shadow รันอยู่ และ live ต้องใช้ `config/live.yaml` |
| เวลาเครื่องเพี้ยนไม่เกินครึ่งหนึ่งของ recvWindow | ตรวจ `timedatectl` / chrony |
| คีย์ไม่มีสิทธิ์ถอน, จำกัด IP, เปิด Futures | แก้ที่หน้า API Management |
| Single-Asset, one-way, ค่า maker fee จริงไม่สูงกว่า config | แก้บัญชี หรือแก้ `account.fee_maker_bps` |
| ตั้ง ISOLATED + leverage 2 ได้ทุก symbol | symbol ที่ตั้งไม่ได้จะถูกตัดออก (ดู log `symbol_setup_failed`) |

เมื่อเริ่มสำเร็จจะเห็น log `live_symbols_selected` (symbol ที่เลือก) และ `real_venue_ready`

**ให้เริ่มด้วย symbol เดียวก่อน** เฝ้าดูชั่วโมงแรกอย่างใกล้ชิด แล้วค่อยเพิ่ม allow-list (ต้องรีสตาร์ต live เพื่อให้ config ใหม่มีผล)

หยุดแบบปกติ: กด `Ctrl-C` ใน tmux (บอทจะยกเลิกคำสั่ง `rb-*` ทั้งหมดและปิด dead-man ก่อนออก)
**position ที่เปิดอยู่จะไม่ถูกปิดอัตโนมัติ** ถ้าต้องการปิดให้ใช้ kill switch (§4) หรือ `flatten_all.py` ด้านล่าง

### 10.5 สิ่งที่บอททำเองระหว่าง live

- **shadow twin:** มี paper venue ชื่อ `shadow` quote symbol เดียวกับ live ใน process เดียวกัน ใช้ทำ gap report
  ถ้า twin เข้า HALTED จะไม่กระทบ live และไม่ทำให้รีสตาร์ตไม่ได้ แต่ kill switch จะหยุดทั้งคู่
- **hedger (M6):** ถ้า net beta exposure เกิน `hedge_threshold_usdt` (25) จะส่งคำสั่ง hedge แบบ post-only
  ด้วยค่า default จะ hedge ด้วย **ETHUSDT** เพราะ min notional ของ BTCUSDT (100 USDT) เกิน `max_position_usdt` (50)
  hedger ทำงานเฉพาะตอน risk state เป็น OK ถ้าไม่มี instrument ไหนใช้ได้จะเห็น `hedge_too_coarse` ใน log ซึ่งยังปลอดภัย เพราะ risk engine คุม `max_net_beta_exposure_usdt` อยู่
- **beta:** คำนวณใหม่ทุก 1 ชม. จาก klines 1 นาทีย้อนหลัง 3 วัน (log `beta`) ถ้าดึงไม่ได้จะใช้ค่าเดิม
- **หมุน universe (M7):** ทุก 5 นาที
  - demote symbol ที่ live edge ของ 500 fills ล่าสุดมี `mean + 1·SE < 0` แล้วเข้า cooldown 24 ชม.
  - symbol ที่หลุดจาก universe ของ screener จะกลับเป็น CANDIDATE
  - ถ้ามีช่องว่าง จะ promote symbol ใหม่ตามกฎ §10.3 โดยตั้ง ISOLATED + leverage บนตลาดก่อนเริ่ม quote
  - symbol ที่ถูกเอาออกจะ quote เฉพาะฝั่งที่ลด position จนกว่าจะ flat
  - ทุกการเปลี่ยนแปลงถูกบันทึกใน `universe_decisions`, log `universe_decision` และส่ง alert INFO
- **ไม่มีการเพิ่มขนาดอัตโนมัติ** ทุกการเพิ่มขนาดต้องทำตาม §6

### 10.6 งานประจำวันเพิ่มเติมสำหรับ live

1. ทำ §7 ทั้งหมด ยกเว้น Grafana: Prometheus ใน compose scrape เฉพาะ shadow และ demo จึง**ยังไม่เห็น live ใน Grafana**
   ให้ดูสถานะ live ด้วย Telegram `/status` (บอท #3) หรือ `curl -s localhost:9110/metrics | grep -E "^rb_(risk_state|equity_usdt)"`
2. เปิด `reports/live/YYYY-MM-DD/report.md` แล้วดู:
   - หัวข้อ **Shadow-vs-live gap**: ถ้า edge gap ติดลบมาก (เกิน 2·SE) หรือ fill rate ของ live ต่ำกว่า shadow มาก แปลว่า paper fill model มองโลกในแง่ดีเกินไป ควรระวังก่อนเพิ่มขนาด
   - หัวข้อ **Universe decisions**: การ promote/demote ของเมื่อวาน
   - PnL decomposition และคอลัมน์ hedge
3. ดู gap report ช่วงหลายวัน:
   ```bash
   uv run python -m riverbank.analytics.gap --db state/live/riverbank.sqlite --start 2026-10-20 --end 2026-10-26
   ```
4. ดูประวัติการหมุน universe:
   ```bash
   sqlite3 state/live/riverbank.sqlite \
     "select datetime(ts/1000,'unixepoch'),symbol,from_state,to_state,reason,n,round(mean,2),round(se,2) from universe_decisions order by id desc limit 20"
   ```

### 10.7 หยุดฉุกเฉินและเครื่องมือ standalone สำหรับ live

ใช้ kill switch ตาม §4 ก่อนเสมอ ถ้าบอทค้างหรือตายไปแล้ว:

```bash
set -a; source ~/.riverbank/live.env; set +a
export MODE=live LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
uv run python tools/cancel_all.py --live                  # dry run: คำสั่ง rb-* ที่ค้างบน production
uv run python tools/cancel_all.py --live --execute        # ยกเลิกจริง
uv run python tools/flatten_all.py --live                 # dry run: position
uv run python tools/flatten_all.py --live --execute       # ปิดด้วย reduce-only limit (--taker เฉพาะฉุกเฉินจริง)
```

กู้คืนจาก HALTED ทำตาม §5 แต่ใช้ DB ของ live:

```bash
sqlite3 state/live/riverbank.sqlite \
  "select datetime(ts/1000,'unixepoch'),level,rule,detail,state_after from risk_events order by ts desc limit 20"
uv run python tools/clear_halt.py --db state/live/riverbank.sqlite --kill-file KILL   # จะถามยืนยัน
```

ระวัง: `clear_halt.py` จะลบไฟล์ `KILL` ซึ่ง shadow ใช้ร่วมกัน ถ้า shadow มี HALT ของตัวเองบันทึกไว้ ต้องล้างแยกด้วย `tools/clear_halt.py` (ค่า default)

### 10.8 การทดสอบรับมอบ M5–M7

| Milestone | เกณฑ์ (SPEC §12) | ดูจากที่ไหน |
|---|---|---|
| M5 | รัน live 7 วันโดยไม่เกินเพดานความเสี่ยงใดเลย และมี shadow-vs-live gap report | `risk_events` ต้องไม่มี HALT, รายงานรายวัน 7 ฉบับ, `analytics.gap` ช่วง 7 วัน |
| M6 | net beta exposure อยู่ในเพดานทั้งใน replay และ live | replay: `tests/replay/test_replay_hedge.py`; live: `risk_events` ไม่มี soft breach `net_beta_exposure` ค้างนาน |
| M7 | universe หมุนอัตโนมัติ และมีบันทึกการตัดสินใจใน log | `universe_decisions` และ log `universe_decision` |
