# Paperasse — build brief (internal, Hack Apertus 2026, Track 2B)

**One sentence:** Swiss papers in, a report out, then a conversation. Drop a folder of Swiss
papers (QR-bills, bank statements, official letters, in fr/de/it); Apertus 1.5 and plain code turn
them into verified facts, a monthly report in your language, and answers to your questions,
with every figure traced to the paper it came from and a blank instead of a guess.

**Hard rules for everyone writing code here**
- Fresh code only. Nothing copied from any other project. No other product name anywhere.
- Apertus 1.5 is the ONLY language model. No other model family may be named anywhere in the repo (a guard test fails if one is).
  must not appear in the repo (a test fails if they do). Other models are not even mentioned.
- The model is reached through an OpenAI-compatible endpoint from env:
  `LLM_NAME` (default `swiss-ai/Apertus-v1.5-70B`), `LLM_BASE_URL`, `LLM_API_KEY`.
  The endpoint for dev is Infomaniak (Swiss sovereign cloud); the same code must run against a
  local OpenAI-compatible server with Apertus 8B (air-gapped). Never hard-code a URL or a key.
  For local runs: `. ./bin-env.sh` at the repo root exports the three variables (file is git-ignored).
- Python 3.12, standard library first. Allowed third-party: pymupdf, zxing-cpp, segno, reportlab,
  fastapi, uvicorn, httpx, pillow, pytest, python-multipart. Nothing else without a reason in a comment.
- Synthetic data only. Fictitious people, companies, addresses, IBANs (valid checksum, bank
  clearing 30000–31999 for QR-IBAN or any 5-digit for plain IBAN). No real person, no real paper.
- Every number the model writes is checked by code against the facts. If a check fails, the field is
  blank with a reason. Code does arithmetic; the model does language.
- French comments and UI by default; the UI must switch fr/de/it/en. Swiss German spelling (no ß).
- Zero network at runtime except the LLM endpoint. No telemetry, no CDN, no web fonts.
- Keep the repo under 100 MB in `data/`. Target: 100 papers ≈ 10 MB.

**Repo layout (track_2b/ is the project root as the template demands)**
```
track_2b/
  Makefile            make run (docker compose up --build), make data, make eval, make test
  docker-compose.yml  one service `paperasse`, port 8640, env from .env, volume ./papers:/papers
  Dockerfile          python:3.12-slim, pip install -r requirements.txt, uvicorn paperasse.web:app
  requirements.txt
  src/paperasse/
    __init__.py
    schema.py         dataclasses + JSON (de)serialisation of Paper, Facts, Field, Check (shared, Agent A owns)
    swissqr.py        Swiss QR-bill payload build + parse (SPC 0200 ...), IBAN/QR-IBAN checksum, QRR reference mod-10 (Agent A owns)
    generate.py       synthetic papers → data/papers/*.pdf + data/truth/*.json (Agent A owns)
    llm.py            OpenAI-compatible client, JSON answer, retries, token counter (Agent B owns)
    read.py           PDF text + QR decode → classify → Apertus extraction → code verification → Facts (Agent B owns)
    verify.py         the checks: QR cross-check, arithmetic, dates, IBAN, amounts, numeric grounding of prose (Agent B owns)
    baseline.py       Apertus alone on the raw text, no checks (the Baseline row) (Agent B owns)
    eval.py           baseline vs ours on data/, writes docs/results.md + docs/results.json (Agent B owns)
    report.py         monthly report: code tables + Apertus prose, grounded (Agent C owns)
    ask.py            questions over facts: SQLite FTS5 retrieval + Apertus answer with citations, grounded (Agent C owns)
    web.py            FastAPI, one page, inline HTML/JS/CSS, no external assets (Agent C owns)
    i18n.py           UI strings fr/de/it/en (Agent C owns)
  tests/              pytest (each agent adds tests for its files; test_guard.py = forbidden strings, Agent B)
  data/papers/        generated PDFs (committed, small)
  data/truth/         ground truth JSON per paper (committed)
  data/questions.json 50 questions with truth answers (Agent A)
  docs/               architecture.svg, results.md, BRIEF.md (this file), screenshots
  technical_report.md the report (filled at the end)
```

**Shared schema (schema.py, Agent A writes it FIRST within 20 minutes; B and C read it)**
```python
Kind = Literal["qr_bill", "bank_statement", "official_letter"]
Lang = Literal["fr", "de", "it"]

@dataclass
class Field:            # one extracted value with its provenance
    value: str | float | None   # None = blank
    status: Literal["verified", "model", "blank"]   # verified = code-checked; model = model only; blank = refused
    reason: str | None = None   # why blank / what check failed, short, in English (code-level)
    source: str | None = None   # where it was checked: "qr", "text", "arithmetic", "iban", "date"

@dataclass
class Facts:            # keys depend on kind; all keys below exist (Field) for the kind
    # qr_bill: issuer, recipient, amount_chf, currency, iban, reference, reference_type, invoice_number, invoice_date, due_date, purpose
    # bank_statement: bank, account_iban, holder, period_start, period_end, opening_balance, closing_balance, movements (list of {date,label,amount}), currency
    # official_letter: authority, recipient, letter_date, subject, letter_kind (tax_decision|payment_summons|premium_notice|commune_notice|permit_decision), amount_chf, deadline, reference_number, action_required
    fields: dict[str, Field]

@dataclass
class Paper:
    id: str             # p001 …
    file: str           # relative path, e.g. data/papers/p001.pdf
    kind: Kind
    language: Lang
    pages: int
    text: str           # extracted text (ours fills it)
    qr_payload: str | None
    facts: Facts
    traps: list[str]    # generator-only: which distractors this paper carries (truth json)
```
Truth JSON (data/truth/p001.json) = `{"id","file","kind","language","traps","facts": {key: plain value}}` with plain
values (no Field wrapper): amounts as floats in CHF, dates ISO `YYYY-MM-DD`, movements as list of {date,label,amount} signed.

**Traps the generator must plant (so that the baseline fails and the checks earn their keep)**
- a second amount in the text (acompte / Teilzahlung / acconto already paid, or a "montant total TTC vs HT")
- a date in the footer or letterhead that is not the invoice date
- a creditor name in the text that differs slightly from the QR (the QR is the truth)
- a bank statement whose movements don't sum to the closing balance on 5 papers (truth says `balanced: false`; ours must blank the closing balance with reason "arithmetic")
- an official letter with no amount at all (model must leave amount blank)
- numbers written Swiss style: `1'234.50`, `CHF 1 234.50`, `Fr. 1234.—`
- an invoice without QR (reference_type NON) → iban/reference from text only → status "model" at best, unless checksum verifies the IBAN
Distribution: 40 qr_bill, 30 bank_statement, 30 official_letter; languages ~ 40 fr / 40 de / 20 it; dates Aug–Oct 2026.

**Questions (data/questions.json, 50)** templated from truth in fr/de/it, each with a `truth` (number, date or list of ids) and `type` (amount|date|list|count):
"Combien dois-je encore à {issuer} ?", "Quel est le solde du compte au {period_end} ?", "Welche Rechnungen sind am {date} fällig?",
"Quante fatture ho ricevuto in settembre?", "Quel est le délai de {authority} ?", "Total des factures reçues en septembre ?"

**Metrics (eval.py)** per numeric/date/id field over all papers, Baseline vs Ours:
- correct %, invented % (a value that differs from truth), blank %. Headline = invented %.
- Questions: exact match % on the 50 questions, Baseline (raw text of all papers in the prompt, no tools) vs Ours.
- Token and time per paper.
Report both as a markdown table in docs/results.md, plus docs/results.json.

**Ports and names:** web on 8640. Container name `paperasse`. Drop folder `./papers` on the host.

**UI (web.py)** single page, plain and calm, black text on white, one accent (Swiss red #d52b1e) for the
verified tick, grey for blanks: left a list of papers with kind badge + language; centre the facts of the
selected paper with a tick (verified), a dot (model only) or a blank with its reason; a "Rapport" tab
with the month's report (language switch fr/de/it/en) ; an "Ask" box at the bottom. Buttons ≥ 40 px.
No colour on the report itself beyond black and the one accent. Works at 375 px wide.

**Report (report.py)** for a chosen month: tables built by code (received bills with due dates and open amount,
bank movements in/out and closing balance per account, letters requiring action with deadlines), then ONE short
prose paragraph by Apertus in the chosen language whose every number is checked by `verify.numbers_grounded`
(every digit-group in the prose must appear in the tables; otherwise the sentence is dropped and a note says so).

**Ask (ask.py)** retrieval over a SQLite FTS5 index of facts + text (no vector DB), top 6 papers to Apertus with their ids,
answer must cite `[p0xx]`; numbers in the answer checked against the cited papers' facts; uncited or
ungrounded numbers → answer replaced by "Je ne trouve pas ce chiffre dans vos papiers." style line in the UI language.
If retrieval finds nothing, no model call: answer "Aucun papier ne répond à cette question."

**Baseline (baseline.py)** Apertus given the raw text (no QR decode, no checks) and asked for the same JSON;
whatever it says is taken as is. For questions: all papers' raw text in one prompt, no citations required.
