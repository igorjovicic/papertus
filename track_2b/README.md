# Paperasse — Swiss papers in, a report out, then a conversation

Drop a folder of Swiss papers (QR-bills, bank statements, official letters, in French, German or Italian)
and Paperasse turns them into verified facts, a monthly report in your language and answers to your
questions. Apertus 1.5 reads and writes the language; plain code decodes the QR, checks every checksum,
redoes every sum and compares every date, so that each figure on screen points back to the paper it came
from, and a field the code cannot confirm stays blank with its reason instead of becoming a guess.
Nothing leaves the machine except the calls to the model endpoint you configure, which can be a Swiss
sovereign cloud or an Apertus 8B server on your own network with no internet at all.

![Architecture](docs/architecture.svg)

Track 2B — Hack Apertus 2026 (online). Target architecture: **c) sovereign Swiss cloud** for the model
endpoint by default, **b) air-gapped** with a local Apertus 8B server (see below). Everything else runs
in one container on your own machine.

## Quick start

```bash
cd track_2b
cp .env.example .env     # then set LLM_NAME, LLM_BASE_URL, LLM_API_KEY
make run                 # docker compose up --build
open http://localhost:8640
```

The `.env` file is optional: the same three variables can come from the shell instead, and a value
exported in the shell wins over the file.

```bash
export LLM_NAME=swiss-ai/Apertus-v1.5-70B
export LLM_BASE_URL=https://<your-endpoint>/v1
export LLM_API_KEY=<your key>
make run
```

The build pulls `python:3.12-slim-bookworm` and the pinned packages of `requirements.txt`; nothing else
touches the network at build time, and at run time only `LLM_BASE_URL` is contacted.

On the first start the `./papers` folder is empty, so the 100 synthetic papers of `data/papers` are
copied into it and read one after the other (the page shows the progress; each paper is read once and
cached by file hash in `runtime/cache.json`). Then:

- **Papiers**: the list on the left (kind badge, language, file name), the facts of the selected paper
  in the centre: a red tick for a value checked by code, a dot for a value only the model said, a blank
  with its reason when the code refused it. Every paper opens as PDF.
- **Rapport**: pick a month and a language (fr / de / it / en). The tables are built by code; the short
  paragraph under them is written by Apertus from those tables, and every number in it is checked.
- **Question** (bottom box): "Combien dois-je encore à … ?", "Welche Rechnungen sind am 20.10.2026 fällig?",
  "Quante fatture ho ricevuto in settembre?" Simple questions are answered by code; the others go to
  Apertus with the six closest papers and must cite them as `[p0xx]`, each citation a link to the PDF.
- Drop your own folder on the page (a whole folder or many files; PDF only). They are saved in `./papers`
  on your machine and read the same way.

Without Docker: `make dev` starts the same server with reload on port 8640 (uses the repo's `.venv` if
present; `PAPERS_DIR` defaults to `./papers`).

Other targets: `make data` regenerates the synthetic papers (seed 42), `make eval` writes
`docs/results.md` (baseline vs ours), `make test` runs the tests (the tests that call the model are
skipped when `LLM_API_KEY` is not set).

## Air-gapped use

The model is reached through an OpenAI-compatible endpoint named by three environment variables and
nothing else. Run Apertus 1.5 8B on the host with vLLM (or any OpenAI-compatible GGUF server) on port
8080 and set in `.env`:

```
LLM_NAME=swiss-ai/Apertus-v1.5-8B
LLM_BASE_URL=http://host.docker.internal:8080/v1
LLM_API_KEY=
```

`docker-compose.yml` maps `host.docker.internal` to the host gateway on Linux too. The page loads no
font, script or style from the network; there is no telemetry. The only outbound connection of the
container is to `LLM_BASE_URL`.

## What is checked by code, what is written by the model

| Step | Who | How |
|---|---|---|
| PDF text, QR code | code | pymupdf, zxing-cpp |
| Kind of paper, language | code | word rules |
| Candidate facts (issuer, dates, amounts, movements…) | Apertus | JSON extraction from the text, `null` when not printed |
| Amount, IBAN, reference, creditor of a QR-bill | code | the QR payload is the truth; IBAN mod 97, QRR mod 10, SCOR mod 97 |
| Dates | code | must appear in the text, due date after invoice date, deadline after letter date |
| Bank statement | code | opening + movements = closing to the centime, else the closing balance is blank (`arithmetic`) |
| Amount on an official letter | code | must be printed on the paper, else blank |
| Report tables and totals | code | `report.py` |
| Report paragraph | Apertus | from the tables as JSON; a sentence with a number not in the tables is dropped and a note says so |
| Simple questions (sum due, balance at a date, count in a month, deadline of an authority) | code | `ask.answer_from_facts` |
| Other questions | Apertus | SQLite FTS5 retrieval, top 6 papers, answer must cite `[p0xx]`; any number not in the cited facts, or no citation, replaces the answer by "I can't find that figure in your papers" |

`docs/results.md` reports the baseline (Apertus alone on the raw text, no checks) against this pipeline:
correct, invented and blank percentages per field, exact match on 50 questions, tokens and time per paper.

## Data

`data/papers` holds 100 synthetic PDFs (40 QR-bills, 30 bank statements, 30 official letters;
~40 fr / 40 de / 20 it; August–October 2026) with fictitious people, companies, addresses and IBANs
(valid checksums, no real bank). `data/truth` holds the ground truth per paper, `data/questions.json`
the 50 questions. No real person, no real paper.

## Licences

- Code: Apache-2.0
- Documentation (`docs/`, `technical_report.md`, this file): CC-BY-4.0
- Data (`data/`): CDLA-Permissive-2.0
