<div align="center">

# 🇹🇭 ThaiCite

### ประตูอนุมัติการอ้างอิงทางวิชาการ สำหรับ AI Agent ทุกตัว

**Context in → Verified Citation List out**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Status](https://img.shields.io/badge/status-v1%20build%2C%20unit--tested%2C%20live%20revalidation%20pending-orange)](./docs/KNOWN_ISSUES.md)
[![Honesty](https://img.shields.io/badge/tier-Dr%20(plausible%2C%20unproven)-red)](./ARCHITECTURE.md)
[![Docs](https://img.shields.io/badge/docs-ARCHITECTURE.md-2ea44f)](./ARCHITECTURE.md)
[![Thai](https://img.shields.io/badge/🇹🇭%20Thai--first-federation-ED1C24)](#-ครอบคลุมงานไทยแบบไม่ใช่ผลพลอยได้)

</div>

---

## 🚨 ปัญหาที่ทุกคนรู้ แต่ยังไม่มีใครปิดให้สนิท

AI เขียนงานวิชาการได้เร็วขึ้นทุกวัน — แต่ก็ **แต่ง citation ขึ้นมาเองได้แนบเนียนขึ้นทุกวันเช่นกัน**

ที่แย่กว่า "ชื่อ paper ปลอม" คือกรณีที่อันตรายกว่านั้นมาก:

> paper มีจริง 100% — DOI จริง ผู้แต่งจริง ตีพิมพ์จริง
> แต่ถูกเอาไปอ้างสนับสนุน claim ที่ **มันไม่เคยพูดถึงเลย**

นี่คือ **citation misuse** ไม่ใช่ hallucination แบบที่ทุกคนคุ้นเคย — และเครื่องมือส่วนใหญ่ในตลาดยังไม่ได้ถูกออกแบบมาเพื่อจับมันโดยตรง

## 💡 สิ่งที่เราเสนอ ไม่ใช่ "ค้นงานวิจัยเก่งกว่า" — แต่คือ "ไม่ปล่อยให้ AI ตัดสินใจแทนหลักฐาน"

Elicit, Consensus, Scite — ค้นเก่ง อ่านเก่ง จัดกลุ่มเก่งอยู่แล้วในปี 2026 เราไม่แข่งกับพวกเขา และจะไม่มีวันแข่งเรื่อง corpus ขนาด 100+ ล้าน paper

สิ่งที่เรายืนหยัดทำคือคำถามแคบมาก แต่ยังไม่มีใครในตลาด (เท่าที่ตรวจสอบได้ ณ วันนี้) ทำให้ครบทั้งระบบ:

> **"จากหลักฐานที่เข้าถึงได้จริง ตอนนี้ — แหล่งนี้มีสิทธิ์ถูกใช้เป็น citation สำหรับ claim นี้โดยเฉพาะ หรือยัง?"**

ไม่ใช่ "paper นี้ดีไหม" — แต่คือ **"การใช้ paper นี้กับ claim นี้ ผ่านเกณฑ์หรือเปล่า"**

paper เดียวกัน คนละ claim ให้ผลต่างกันได้จริง:

```text
Source X  ×  Claim A  →  ✅ ADMIT     หลักฐานรองรับ
Source X  ×  Claim B  →  ❌ REJECT    หลักฐานขัดแย้ง/ไม่รองรับ
Source X  ×  Claim C  →  ⏸️  HOLD      หลักฐานที่มียังไม่พอตัดสิน
```

`HOLD` ไม่ใช่ error — มันคือคำตอบที่ซื่อสัตย์ที่สุดเมื่อข้อมูลไม่พอ ระบบยึดหลัก **`0 ≠ ⊥`** ตลอดทั้งสถาปัตยกรรม: **"หาไม่พบ" ไม่เคยเท่ากับ "ไม่มีอยู่จริง"**

---

## ⚖️ หลักการเดียวที่ไม่มีการต่อรองเด็ดขาด

> ## AI ไม่ใช่แหล่งข้อมูล
> ## AI is not a source.

AI ช่วยตีความ context, ขยายคำค้น, แปลไทย↔อังกฤษ, จัดอันดับ candidate ได้เต็มที่ไม่จำกัด — แต่ **ไม่มีสิทธิ์สร้าง DOI, ชื่อบทความ, ผู้แต่ง, ปี, วารสารขึ้นมาเอง** และไม่มีสิทธิ์ประกาศ `ADMIT` ด้วยความมั่นใจของตัวมันเองล้วน ๆ

```text
API   →  บอกว่าโลกภายนอกมีอะไรจริง
LLM   →  ตีความความหมาย เทียบ claim กับ evidence
GATE  →  ตัดสินแบบ deterministic — เขียนเป็น rule ไม่ใช่ "ความรู้สึกว่าใช่"
```

`decision` **ไม่เคย** เป็นสิ่งที่ LLM ตั้งเอง แม้แต่ครั้งเดียว — นี่คือกำแพงที่สำคัญที่สุดในทั้งระบบ

```text
ไม่มี source record   →  ไม่มี candidate
ไม่มี candidate        →  ไม่มี canonical work
ไม่มี canonical work   →  ไม่มีสิทธิ์ ADMIT
```

นี่ไม่ใช่ prompt ที่บอกว่า "อย่ามั่วนะ" — แต่เป็น **ข้อบังคับระดับสถาปัตยกรรมซอฟต์แวร์** ที่ตรวจสอบได้จริงในโค้ด

---

## 🌐 สถาปัตยกรรมโดยย่อ

```text
                        CONTEXT
                           │
                           ▼
                CONTEXT CONTRACT (frozen ก่อนค้น)
                           │
            ┌──────────────┼───────────────┐
            ▼              ▼               ▼
      🌍 GLOBAL TRACK  🇹🇭 LOCAL TRACK   🩺 DOMAIN TRACK
      OpenAlex         ThaiJO           PubMed / PMC
      Crossref         TNRR
            │              │               │
            └──────────────┼───────────────┘
                           ▼
                  SOURCE-NATIVE RECORD
                           │
                           ▼
                   EVIDENCE CAPSULE
      (identity · passage · locator · hash · timestamp)
                           │
                           ▼
              CLAIM ↔ EVIDENCE RELATION
       SUPPORTS · CHALLENGES · CONTEXT_ONLY · UNCLEAR
                           │
                           ▼
             ⚖️  ADMISSION GATE (deterministic)
              ┌────────────┼─────────────┐
              ▼            ▼             ▼
           ✅ ADMIT     ❌ REJECT      ⏸️ HOLD
                           │
                           ▼
                      CITATION LIST
```

รายละเอียดฉบับเต็ม — รวมประวัติการออกแบบทุกรอบ, การเทียบกับ Elicit/Consensus/Scite/ResearchRabbit ตรง ๆ, และการตรวจสอบ prior art อย่างตรงไปตรงมา — อยู่ใน **[`ARCHITECTURE.md`](./ARCHITECTURE.md)**

---

## 🇹🇭 ครอบคลุมงานไทยแบบไม่ใช่ผลพลอยได้

ตลาดโลกยังไม่มีใครทำ ThaiJO + TNRR + TCI ให้เป็น **first-class source เทียบเท่า** PubMed/OpenAlex/Crossref — ส่วนใหญ่ Thai literature เป็นแค่ "ติดไปด้วยถ้ามี index"

เราแบ่ง "งานเกี่ยวกับประเทศไทย" เป็น **4 ระดับที่ไม่แยกกันเด็ดขาด** (record เดียวติดได้หลายป้าย):

| ป้าย | ความหมาย |
|---|---|
| 🇹🇭 **THAI_LANGUAGE** | เนื้อหาเป็นภาษาไทย |
| 🏛️ **PUBLISHED_IN_THAILAND** | ตีพิมพ์/สังกัดสถาบันไทย (ThaiJO, TNRR, TCI) |
| 📍 **ABOUT_THAILAND** | ศึกษาประชากรหรือพื้นที่ในประเทศไทย |
| 🌏 **FOREIGN_ABOUT_THAILAND** | งานต่างชาติที่ศึกษาไทย โดยไม่มีผู้แต่ง/วารสารไทย |

```text
LOCAL_EVIDENCE_NOT_FOUND   ≠   NO_LOCAL_EVIDENCE_EXISTS
```

"ไม่พบหลักฐานงานไทยในการค้นครั้งนี้" **ไม่เคย** แปลว่า "ไม่มีงานไทยเรื่องนี้อยู่จริง" — เราซื่อสัตย์กับความไม่สมมาตรของ coverage เสมอ

---

## 🔬 ซื่อสัตย์กับข้อจำกัดของตัวเอง — ตั้งแต่วันแรก

เราไม่ขายว่า "นวัตกรรมล้ำโลก" ในทุกองค์ประกอบ เพราะไม่จริง — RefLens, CiteWell, CiteGuard-RAG, Scite ล้วนมี evidence-grounded citation verification ในรูปแบบใดรูปแบบหนึ่งอยู่แล้ว เราตรวจสอบ prior art อย่างตรงไปตรงมาและบันทึกไว้ในเอกสาร (ดู `ARCHITECTURE.md` ภาคที่ 5) แทนที่จะโฆษณาเกินจริง

สิ่งที่เรายืนยันว่ายังไม่มีใครทำครบคือ **หน่วยที่ตรวจ**: `Citation Use = Claim × Source × Evidence × Context` แทนที่จะ verify ที่ระดับ paper เฉย ๆ — และการทำให้ Thailand เป็น first-class citizen ของ pipeline ไม่ใช่ afterthought

> **ระดับความมั่นใจปัจจุบัน (มาตรวัดของโปรเจกต์เอง): `Dr` — สถาปัตยกรรมที่มีเหตุผลรองรับ ผ่าน unit test 218/218 ตัว (รวมการทดสอบเชิงรุกภาษาไทย 5 รอบ ที่พบและแก้บั๊กร้ายแรงจริงทุกรอบ รวมถึงการแยก Discovery/Authorization แบบโครงสร้าง, False-ADMIT จาก semantic opposite, และรอบ 5 ที่เปลี่ยนบทบาทเป็น AI Scout/Reader + deterministic Gate — ดู [`docs/KNOWN_ISSUES.md`](./docs/KNOWN_ISSUES.md) สำหรับข้อจำกัดที่ยังเหลืออยู่แบบตรงไปตรงมา รวมถึงบั๊กจริงที่รอบ 5 พบในตัวเอง) แต่ยังไม่ใช่ "ระบบที่พิสูจน์แล้วว่าดีกว่า" จนกว่าจะผ่าน live adversarial test เต็มรูปแบบ**

การทดสอบเชิงรุก (adversarial) 100 สถานการณ์ครั้งแรกพบ **จุดบกพร่องสำคัญจริง** ในกลไกตรวจความเกี่ยวข้อง — เรา**ไม่ซ่อน**ผลลัพธ์นั้น และตอนนี้แก้ไขแล้ว (พิสูจน์ด้วย unit test) แต่ยังไม่ได้ทดสอบซ้ำแบบ live เต็ม 100 ครั้ง เพราะ OpenAlex ยัง rate-limit อยู่ ทุกรายงาน ทั้งที่ผ่านและไม่ผ่าน อยู่ใน [`tests/golden/`](./tests/golden/) และ [`docs/KNOWN_ISSUES.md`](./docs/KNOWN_ISSUES.md) แบบเปิดเผยทั้งหมด นี่คือหลักการเดียวกับที่เราบังคับใช้กับ citation ของผู้อื่น — ใช้กับตัวเราเองด้วย

---

## 🗺️ Roadmap v1 — 5 สิ่งที่ต้องสร้างก่อน ไม่ใช่ 50 อย่าง

1. ✅ **Citation-Use object** — verify ต่อ claim ไม่ใช่ต่อ paper — **สร้างแล้ว** (`core/models.py: CiteUse`)
2. ✅ **Evidence Capsule / Proof-carrying cite** — พก passage + locator + hash เสมอ — **สร้างแล้ว**
3. ✅ **ADMIT–REJECT–HOLD** deterministic gate — **สร้างแล้ว** (`evidence/verifier.py: gate_admission_decision`)
4. ✅ **Support × Challenge + Global × Local** search — **สร้างแล้ว** (`routing/router.py`, `routing/query_planner.py`, Thai-first ordering)
5. ⏸️ **Fail-able negative-control benchmark ระดับเต็ม (live 100 scenarios)** — ยังรอ เพราะ OpenAlex ยัง rate-limit อยู่ และตามคำสั่งให้สร้างระบบเต็มก่อนแล้วค่อยเทส — ดูสถานะล่าสุดที่ [`docs/KNOWN_ISSUES.md`](./docs/KNOWN_ISSUES.md)
6. ✅ **Scout/Reader role change (MCP 3-primitive surface)** — `resolve_source`/`fetch_evidence`/`check_claim_evidence`, AI ทำงานเชิงความหมาย ThaiCite ทำหน้าที่ deterministic Gate เท่านั้น — **สร้างแล้ว รอบ 5** (`docs/ARCHITECTURE_NOTE.md`, `docs/KNOWN_ISSUES.md`)

**Adapter จริงที่ใช้งานได้แล้ว:** OpenAlex, Crossref, PubMed (ยืนยันด้วย live request จริง) — ThaiJO ปรับสถาปัตยกรรมรอบ 3 (2026-09-20) เป็น **Harvester + Local Index**: OAI-PMH เป็น harvesting protocol ไม่ใช่ search API, จึงแยก `adapters/thaijo_harvester.py` (ดึงข้อมูลจริงทีละ endpoint ด้วย subdomain URL ที่แก้ไขแล้ว เช่น `https://sc01.tci-thaijo.org/index.php/index/oai` — ยืนยันสดแล้วว่า reachable ด้วย `?verb=Identify` HTTP 200) เก็บลง SQLite+FTS5 local index (`adapters/thaijo_index.py`) แล้วให้ `adapters/thaijo.py::ThaiJOAdapter` เป็น thin wrapper ค้นจาก local index แทนการยิง network ทุกครั้งที่ search — **ต้องรัน `python -m thaicite.adapters.thaijo_harvester --sync` ก่อนใช้งานจริง** (ดู `docs/ARCHITECTURE_NOTE.md`, KNOWN_ISSUES.md)

**ยังไม่สร้าง (ตั้งใจ):** public ID infrastructure ของตัวเอง, citation graph ของตัวเอง, local corpus ขนาดใหญ่, vector database, UI ใด ๆ, PMC full-text fetcher, TNRR/TCI/TDC/DataCite adapters — ตลาดทำสิ่งเหล่านี้ดีกว่าเราอยู่แล้ว หรือยังไม่ถึงคิวตาม v1 scope

---

## 📁 โครงสร้างโปรเจกต์

```text
thai-cite-engine/
├── README.md              ← ไฟล์นี้
├── ARCHITECTURE.md         ← สถาปัตยกรรมฉบับเต็ม (5 ภาค + การเช็คตลาดจริง + prior art)
├── LICENSE
├── pyproject.toml
├── src/thaicite/
│   ├── core/               engine.py, models.py, harness.py
│   ├── adapters/            openalex.py, crossref.py, thaijo.py, pubmed.py, base.py
│   ├── resolve/              identity.py, conflicts.py
│   ├── evidence/              verifier.py  (G1–G7 evidence gate)
│   └── normalize/              thai_relevance.py
├── tests/
│   ├── fixtures/            adversarial_100.json (100 สถานการณ์ทดสอบเชิงรุก)
│   └── golden/               รายงานผลการทดสอบทุกรอบ — ไม่ซ่อนผลที่ไม่ผ่าน
└── docs/
    └── ARCHITECTURE_NOTE.md  สรุปสถานะ + จุดบกพร่องที่รู้อยู่ตอนนี้
```

---

## 🔌 แหล่งข้อมูลหลัก (v1)

| แหล่ง | บทบาท |
|---|---|
| 🌍 **OpenAlex** | discovery + citation graph ระดับโลก |
| 🔗 **Crossref** | ยืนยัน DOI / metadata |
| 🇹🇭 **ThaiJO** (OAI-PMH) | วารสารไทยโดยตรง |
| 🩺 **PubMed / PMC** | เส้นทาง biomedical + full text |

*TNRR เข้าเป็น adapter ตัวที่ 5 เมื่อมี credential พร้อม — รายละเอียดเต็มดู [`ARCHITECTURE.md §56`](./ARCHITECTURE.md)*

### Environment variables (ไม่บังคับ, ไม่มีค่า default ที่ฝังไว้)

| ตัวแปร | ใช้กับ | ผลถ้าไม่ตั้งค่า |
|---|---|---|
| `THAICITE_CONTACT_EMAIL` | Crossref (`mailto` polite-pool param) | ไม่ส่ง contact email เลย (ไม่มี fallback ที่ hardcode ไว้) |
| `THAICITE_NCBI_API_KEY` | PubMed / NCBI E-Utilities (`api_key` param) | เรียกแบบไม่มี key (ปริมาณต่ำได้ตามปกติ) |

ตัวอย่างการตั้งค่า (แทนที่ด้วยอีเมล/คีย์จริงของคุณเอง อย่าใช้ค่าตัวอย่างนี้จริง):

```bash
export THAICITE_CONTACT_EMAIL="you@example.com"
export THAICITE_NCBI_API_KEY="your-ncbi-api-key"
```

---

## 📜 License

MIT License — ดู [`LICENSE`](./LICENSE)

---

<div align="center">

### ไม่ใช่ AI ที่ hallucinate น้อยลง — แต่คือโครงสร้างที่ไม่ให้งานวิชาการต้องเชื่อโมเดลในจุดที่ไม่จำเป็นต้องเชื่อมันเลย

🇹🇭 **Built for Thailand's research ecosystem — designed to be honest about what it doesn't know yet.**

</div>
