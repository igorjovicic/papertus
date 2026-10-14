"""L'application web : une page, aucune ressource externe, le dépôt de papiers en dossier.

- GET  /                      la page (HTML, CSS et JS en ligne ; polices du système)
- GET  /health                état du service
- GET  /status                avancement de la lecture (lue en boucle par la page)
- POST /papers                dépôt de fichiers (multipart, plusieurs) → lus l'un après l'autre
- GET  /api/papers            tous les papiers (JSON)
- GET  /api/paper/{id}        un papier
- GET  /api/paper/{id}/file   le PDF
- GET  /api/report?month=&lang=   le rapport (tableaux + prose contrôlée), JSON + HTML + Markdown
- GET  /api/report.md?month=&lang=  le rapport en Markdown (téléchargement)
- POST /api/ask {question, lang}  une réponse avec citations
- GET  /api/strings?lang=     les chaînes de l'interface
- GET  /api/months            les mois présents dans les papiers

Au démarrage, chaque PDF déjà présent dans PAPERS_DIR est lu (cache JSON par empreinte
du fichier dans RUNTIME_DIR/cache.json : un papier n'est lu qu'une fois). La lecture
appelle le modèle ; elle se fait dans un fil, l'un après l'autre, et la page montre où
l'on en est.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from . import ask as ask_mod
from . import i18n
from . import report as report_mod
from .schema import Paper

PKG_ROOT = Path(__file__).resolve().parents[2]  # track_2b/
PAPERS_DIR = Path(os.getenv("PAPERS_DIR") or "./papers").resolve()
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR") or (PKG_ROOT / "runtime")).resolve()
CACHE_FILE = RUNTIME_DIR / "cache.json"
SEED_CACHE = PKG_ROOT / "data" / "cache.json"       # lectures livrées avec les papiers d'exemple (facultatif)
EVAL_CACHE = RUNTIME_DIR / "cache" / "ours"        # lectures faites par `make eval` (même chaîne, même modèle)
INDEX_FILE = RUNTIME_DIR / "index.db"
TEXT_PREVIEW = 6000

_ID_RE = re.compile(r"^p\d{3,}$")


# ---------------------------------------------------------------------------
# L'état du service (un processus, un fil de lecture)
# ---------------------------------------------------------------------------

class Store:
    """Papiers en mémoire, cache sur disque, index FTS, file d'attente de lecture."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.papers: dict[str, Paper] = {}
        self.paths: dict[str, Path] = {}
        self.hashes: dict[str, str] = {}          # id → empreinte
        self.cache: dict[str, dict[str, Any]] = {}  # empreinte → Paper.to_dict()
        self.queue: "queue.Queue[Path | None]" = queue.Queue()
        self.status: dict[str, Any] = {"total": 0, "done": 0, "running": False, "current": None, "errors": []}
        self.index: ask_mod.Index | None = None
        self.thread: threading.Thread | None = None

    # --- cache ---------------------------------------------------------------
    def load_cache(self) -> None:
        """Le cache propre, complété par la graine livrée dans data/ (jamais écrasée)."""
        self.cache = {}
        for path in (SEED_CACHE, CACHE_FILE):
            try:
                got = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(got, dict):
                    self.cache.update(got)
            except (OSError, ValueError):
                continue

    @staticmethod
    def from_eval_cache(path: Path, digest: str) -> dict[str, Any] | None:
        """La lecture faite par l'évaluation pour ce même fichier (empreinte et modèle identiques)."""
        candidate = EVAL_CACHE / f"{path.stem}.json"
        try:
            got = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        stamp = str(got.get("stamp", ""))
        model = os.getenv("LLM_NAME") or ""
        if stamp.startswith(digest[:16]) and (not model or stamp.endswith(":" + model)):
            paper = got.get("paper")
            return paper if isinstance(paper, dict) else None
        return None

    def save_cache(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)

    # --- identifiants ---------------------------------------------------------
    def new_id(self, path: Path) -> str:
        stem = path.stem
        if _ID_RE.match(stem) and stem not in self.papers:
            return stem
        used = {int(i[1:]) for i in self.papers if _ID_RE.match(i)}
        n = max(used, default=0) + 1
        return f"p{n:03d}"

    # --- ajout -----------------------------------------------------------------
    def add_paper(self, paper: Paper, path: Path, digest: str) -> None:
        with self.lock:
            self.papers[paper.id] = paper
            self.paths[paper.id] = path
            self.hashes[paper.id] = digest
            if self.index is not None:
                self.index.add(paper)

    def ordered(self) -> list[Paper]:
        with self.lock:
            return [self.papers[k] for k in sorted(self.papers)]

    # --- lecture ---------------------------------------------------------------
    def enqueue(self, paths: list[Path]) -> int:
        n = 0
        for p in paths:
            self.queue.put(p)
            n += 1
        with self.lock:
            self.status["total"] += n
            self.status["running"] = self.status["total"] > self.status["done"]
        return n

    def read_one(self, path: Path) -> None:
        """Lit un PDF (cache d'abord), l'ajoute, met le cache à jour."""
        digest = file_hash(path)
        with self.lock:
            known = next((pid for pid, h in self.hashes.items() if h == digest), None)
        if known:
            return  # même fichier déjà lu (doublon)
        cached = self.cache.get(cache_key(digest)) or self.from_eval_cache(path, digest)
        if cached:
            paper = Paper.from_dict(cached)
            with self.lock:
                if paper.id in self.papers:
                    paper.id = self.new_id(path)
            self.add_paper(paper, path, digest)
            with self.lock:
                if cache_key(digest) not in self.cache:
                    self.cache[cache_key(digest)] = paper.to_dict()
                    self.save_cache()
            return
        from . import read  # import tardif : pymupdf/zxing seulement quand on lit vraiment
        with self.lock:
            pid = self.new_id(path)
        paper = read.read_paper(str(path), paper_id=pid, rel_file=path.name)
        paper.file = path.name
        self.add_paper(paper, path, digest)
        with self.lock:
            self.cache[cache_key(digest)] = paper.to_dict()
            self.save_cache()

    def worker(self) -> None:
        while True:
            path = self.queue.get()
            if path is None:
                break
            with self.lock:
                self.status["current"] = path.name
                self.status["running"] = True
            try:
                self.read_one(path)
            except Exception as e:  # une lecture qui casse ne doit pas arrêter les suivantes
                with self.lock:
                    self.status["errors"].append({"file": path.name, "error": f"{type(e).__name__}: {str(e)[:160]}"})
            finally:
                with self.lock:
                    self.status["done"] += 1
                    self.status["current"] = None
                    self.status["running"] = self.status["total"] > self.status["done"]

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.worker, name="paperasse-reader", daemon=True)
            self.thread.start()


STORE = Store()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def reader_stamp() -> str:
    """Empreinte du code de lecture (read.py + verify.py) et du nom du modèle : une lecture
    en cache ne vaut que pour le code et le modèle qui l'ont produite."""
    h = hashlib.sha256()
    for name in ("read.py", "verify.py", "schema.py"):
        try:
            h.update((Path(__file__).parent / name).read_bytes())
        except OSError:
            continue
    h.update((os.getenv("LLM_NAME") or "").encode())
    return h.hexdigest()[:12]


def cache_key(digest: str) -> str:
    return f"{digest}:{reader_stamp()}"


def scan_papers_dir() -> list[Path]:
    if not PAPERS_DIR.is_dir():
        return []
    return sorted(p for p in PAPERS_DIR.rglob("*.pdf") if p.is_file())


def boot(papers_dir: Path | None = None) -> None:
    """Charge le cache, lance le fil de lecture, met en file les PDF du dossier."""
    global PAPERS_DIR
    if papers_dir is not None:
        PAPERS_DIR = papers_dir
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STORE.load_cache()
    try:
        if INDEX_FILE.exists():
            INDEX_FILE.unlink()
        STORE.index = ask_mod.Index(str(INDEX_FILE))
    except Exception:
        STORE.index = ask_mod.Index(None)
    STORE.start()
    STORE.enqueue(scan_papers_dir())


# ---------------------------------------------------------------------------
# L'application
# ---------------------------------------------------------------------------

async def _lifespan(app: FastAPI):
    boot()
    yield


app = FastAPI(title="Paperasse", lifespan=_lifespan)


def paper_json(p: Paper, *, full: bool = False) -> dict[str, Any]:
    d = p.to_dict()
    if not full and len(d.get("text", "")) > TEXT_PREVIEW:
        d["text"] = d["text"][:TEXT_PREVIEW] + " …"
    d["file"] = STORE.paths.get(p.id, Path(p.file)).name
    return d


@app.get("/health")
def health() -> dict[str, Any]:
    from . import llm  # compteur de jetons du processus, pour la transparence
    with STORE.lock:
        return {"ok": True, "papers": len(STORE.papers), "reading": STORE.status["running"],
                "model": os.getenv("LLM_NAME") or "", "endpoint_set": bool(os.getenv("LLM_BASE_URL")),
                "model_usage": llm.usage_total()}


@app.get("/status")
def status() -> dict[str, Any]:
    with STORE.lock:
        s = dict(STORE.status)
        s["papers"] = len(STORE.papers)
        s["errors"] = s["errors"][-10:]
        return s


@app.get("/api/strings")
def strings(lang: str = "fr") -> dict[str, str]:
    return i18n.all_strings(lang)


@app.get("/api/papers")
def list_papers() -> list[dict[str, Any]]:
    return [paper_json(p) for p in STORE.ordered()]


@app.get("/api/paper/{pid}")
def one_paper(pid: str) -> dict[str, Any]:
    p = STORE.papers.get(pid)
    if p is None:
        raise HTTPException(404, "unknown paper")
    return paper_json(p, full=True)


@app.get("/api/paper/{pid}/file")
def paper_file(pid: str) -> FileResponse:
    path = STORE.paths.get(pid)
    if path is None or not path.is_file():
        raise HTTPException(404, "no file for this paper")
    return FileResponse(str(path), media_type="application/pdf", filename=path.name,
                        content_disposition_type="inline")


@app.get("/api/months")
def months() -> list[str]:
    seen: set[str] = set()
    for p in STORE.ordered():
        for key in ("invoice_date", "due_date", "period_end", "letter_date", "deadline"):
            d = report_mod.as_date(report_mod.field_value(p, key))
            if d:
                seen.add(d.strftime("%Y-%m"))
    return sorted(seen, reverse=True)


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _check_month(month: str) -> str:
    if not _MONTH_RE.match(month or ""):
        raise HTTPException(400, "month must be YYYY-MM")
    return month


@app.get("/api/report")
def api_report(month: str = "2026-09", lang: str = "fr", prose: int = 1) -> dict[str, Any]:
    month = _check_month(month)
    lang = i18n.norm_lang(lang)
    rep = report_mod.build_report(STORE.ordered(), month, lang, with_prose=bool(prose))
    return {"report": rep.to_dict(), "html": report_mod.render_html(rep, lang),
            "markdown": report_mod.render_markdown(rep, lang)}


@app.get("/api/report.md")
def api_report_md(month: str = "2026-09", lang: str = "fr") -> PlainTextResponse:
    month = _check_month(month)
    lang = i18n.norm_lang(lang)
    rep = report_mod.build_report(STORE.ordered(), month, lang)
    return PlainTextResponse(report_mod.render_markdown(rep, lang), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="paperasse-{month}-{lang}.md"'})


@app.post("/api/ask")
async def api_ask(request: Request) -> dict[str, Any]:
    body = await request.json()
    question = str(body.get("question") or "").strip()
    lang = i18n.norm_lang(body.get("lang"))
    if not question:
        raise HTTPException(400, "question is empty")
    answer = ask_mod.ask(question, STORE.ordered(), lang, index=STORE.index)
    d = answer.to_dict()
    d["papers"] = [{"id": i, "kind": STORE.papers[i].kind, "file": STORE.paths[i].name}
                   for i in answer.cited_ids if i in STORE.papers]
    return d


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@app.post("/papers")
async def upload_papers(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """Enregistre les PDF reçus dans le dépôt et les met en file de lecture."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    skipped = 0
    for up in files:
        name = Path(up.filename or "").name
        if not name.lower().endswith(".pdf"):
            skipped += 1
            continue
        data = await up.read()
        if not data.startswith(b"%PDF"):
            skipped += 1
            continue
        name = _SAFE_NAME.sub("_", name)
        target = PAPERS_DIR / name
        if target.exists():
            if file_hash(target) == hashlib.sha256(data).hexdigest():
                saved.append(target)  # même fichier : il sera reconnu comme doublon
                continue
            base, n = target.stem, 2
            while target.exists():
                target = PAPERS_DIR / f"{base}-{n}.pdf"
                n += 1
        target.write_bytes(data)
        saved.append(target)
    STORE.enqueue(saved)
    return {"received": len(saved), "skipped": skipped, "status": status()}


@app.get("/", response_class=HTMLResponse)
def page() -> HTMLResponse:
    return HTMLResponse(PAGE)


# ---------------------------------------------------------------------------
# La page : tout en ligne, noir sur blanc, un seul accent (#d52b1e), gris pour les blancs
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paperasse</title>
<style>
:root { --ink:#111; --paper:#fff; --grey:#777; --line:#e3e3e3; --soft:#f5f5f5; --accent:#d52b1e; }
* { box-sizing:border-box; }
html, body { margin:0; background:var(--paper); color:var(--ink);
  font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; font-size:15px; line-height:1.45; }
a { color:var(--ink); }
a.pid { color:var(--accent); text-decoration:none; border-bottom:1px solid var(--accent); font-variant-numeric:tabular-nums; }
header { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; padding:12px 16px; border-bottom:1px solid var(--line); }
header h1 { font-size:20px; margin:0; letter-spacing:.2px; }
header h1 b { color:var(--accent); }
header .tag { color:var(--grey); flex:1 1 240px; font-size:13px; }
.langs { display:flex; gap:4px; }
button, .btn { min-height:40px; min-width:40px; padding:0 14px; border:1px solid var(--ink); background:var(--paper); color:var(--ink);
  font:inherit; border-radius:4px; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; text-decoration:none; }
button.on, .btn.on { background:var(--ink); color:var(--paper); }
button.accent { background:var(--accent); border-color:var(--accent); color:#fff; }
button:disabled { opacity:.5; cursor:default; }
main { padding:12px 16px 120px; max-width:1200px; margin:0 auto; }
.drop { border:2px dashed var(--line); border-radius:6px; padding:14px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.drop.over { border-color:var(--accent); background:var(--soft); }
.drop .t { flex:1 1 200px; }
.drop .t strong { display:block; }
.drop .t span { color:var(--grey); font-size:13px; }
input[type=file] { display:none; }
.progress { margin:8px 0 0; font-size:13px; color:var(--grey); min-height:18px; }
.progress .err { color:var(--accent); }
.tabs { display:flex; gap:4px; margin:16px 0 12px; border-bottom:1px solid var(--line); }
.tabs button { border:none; border-bottom:3px solid transparent; border-radius:0; padding:0 12px; }
.tabs button.on { background:none; color:var(--ink); border-bottom-color:var(--accent); font-weight:600; }
.panel { display:none; } .panel.on { display:block; }
.cols { display:grid; grid-template-columns:300px 1fr; gap:16px; }
.list { border:1px solid var(--line); border-radius:6px; max-height:70vh; overflow:auto; }
.list .row { display:grid; grid-template-columns:auto 1fr auto; gap:8px; align-items:center; width:100%; min-height:44px;
  padding:6px 10px; border:none; border-bottom:1px solid var(--line); background:none; text-align:left; cursor:pointer; font:inherit; }
.list .row:last-child { border-bottom:none; }
.list .row.on { background:var(--soft); }
.list .row .f { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
.badge { font-size:11px; border:1px solid var(--ink); border-radius:3px; padding:1px 5px; white-space:nowrap; }
.badge.lang { border-color:var(--line); color:var(--grey); }
.list .empty, .facts .empty { padding:16px; color:var(--grey); }
.facts h2 { font-size:17px; margin:0 0 4px; }
.facts .meta { color:var(--grey); font-size:13px; margin-bottom:10px; display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-weight:600; font-size:13px; color:var(--grey); }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr.total td { font-weight:600; border-top:1px solid var(--ink); }
tr.warn td { color:var(--accent); font-size:13px; }
.g { display:inline-block; width:20px; text-align:center; font-weight:700; }
.g.verified { color:var(--accent); }
.g.model { color:var(--ink); }
.g.blank { color:var(--grey); }
td.v { word-break:break-word; }
td.blank { color:var(--grey); }
.reason { color:var(--grey); font-size:12px; }
.muted { color:var(--grey); }
.legend { font-size:12px; color:var(--grey); display:flex; gap:14px; flex-wrap:wrap; margin-top:12px; }
.mov { margin:4px 0; font-size:13px; }
.mov td { padding:2px 6px; border-bottom:1px dotted var(--line); }
details summary { cursor:pointer; color:var(--grey); font-size:13px; }
pre { white-space:pre-wrap; font-size:12px; background:var(--soft); padding:8px; border-radius:4px; max-height:300px; overflow:auto; }
.bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
select { min-height:40px; font:inherit; padding:0 8px; border:1px solid var(--ink); border-radius:4px; background:var(--paper); }
.report #report { overflow-x:auto; }
.report td.d, .report td.num { white-space:nowrap; }
.report h2 { font-size:18px; margin:4px 0 10px; }
.report h3 { font-size:15px; margin:18px 0 6px; }
.report p.prose { border-left:3px solid var(--accent); padding:6px 10px; margin:6px 0; }
.ask { position:fixed; left:0; right:0; bottom:0; background:var(--paper); border-top:1px solid var(--line); padding:10px 16px; }
.ask .in { display:flex; gap:8px; max-width:1200px; margin:0 auto; }
.ask input { flex:1; min-height:40px; font:inherit; padding:0 10px; border:1px solid var(--ink); border-radius:4px; }
.ask .out { max-width:1200px; margin:8px auto 0; font-size:14px; display:none; }
.ask .out.on { display:block; }
.ask .out .st { font-size:12px; color:var(--grey); margin-top:3px; }
.ask .out .st .ok { color:var(--accent); }
@media (max-width:760px) {
  .cols { grid-template-columns:1fr; }
  .list { max-height:34vh; }
  header .tag { display:none; }
  main { padding-bottom:160px; }
}
</style>
</head>
<body>
<header>
  <h1><b>Paperasse</b></h1>
  <div class="tag" data-i18n="app.tagline"></div>
  <div class="langs" id="langs"></div>
</header>
<main>
  <div class="drop" id="drop">
    <div class="t"><strong data-i18n="drop.title"></strong><span data-i18n="drop.hint"></span></div>
    <label class="btn" for="fdir" data-i18n="drop.folder"></label>
    <input type="file" id="fdir" webkitdirectory directory multiple>
    <label class="btn" for="ffiles" data-i18n="drop.files"></label>
    <input type="file" id="ffiles" multiple accept="application/pdf,.pdf">
  </div>
  <div class="progress" id="progress"></div>

  <div class="tabs">
    <button id="tab-papers" class="on" data-i18n="tab.papers"></button>
    <button id="tab-report" data-i18n="tab.report"></button>
  </div>

  <section class="panel on" id="panel-papers">
    <div class="cols">
      <div class="list" id="list"></div>
      <div class="facts" id="facts"></div>
    </div>
  </section>

  <section class="panel report" id="panel-report">
    <div class="bar">
      <label><span data-i18n="month.label"></span> <select id="month"></select></label>
      <button class="accent" id="gen" data-i18n="report.generate"></button>
      <a class="btn" id="md" href="#" data-i18n="report.markdown"></a>
    </div>
    <div id="report"></div>
  </section>
</main>

<div class="ask">
  <form class="in" id="askform">
    <input id="q" type="text" autocomplete="off">
    <button class="accent" type="submit" id="askbtn" data-i18n="ask.button"></button>
  </form>
  <div class="out" id="out"></div>
</div>

<script>
(function () {
  var S = {};                       // chaînes de la langue courante
  var state = { lang: 'fr', papers: [], selected: null, tab: 'papers' };
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  var T = function (k) { return S[k] || k; };
  var LANGS = ['fr', 'de', 'it', 'en'];
  var FIELD_ORDER = {
    qr_bill: ['issuer','recipient','amount_chf','currency','iban','reference','reference_type','invoice_number','invoice_date','due_date','purpose'],
    bank_statement: ['bank','account_iban','holder','period_start','period_end','opening_balance','closing_balance','movements','currency'],
    official_letter: ['authority','recipient','letter_date','subject','letter_kind','amount_chf','deadline','reference_number','action_required']
  };
  function chf(v) { if (v == null || isNaN(v)) return '—'; var s = Math.abs(v).toFixed(2); s = s.replace(/\B(?=(\d{3})+(?!\d))/g, "'"); return (v < 0 ? '-' : '') + s; }

  // --- langue ------------------------------------------------------------
  function setLang(l, first) {
    state.lang = l;
    try { localStorage.setItem('paperasse.lang', l); } catch (e) {}
    document.documentElement.lang = l;
    fetch('/api/strings?lang=' + l).then(function (r) { return r.json(); }).then(function (s) {
      S = s;
      document.querySelectorAll('[data-i18n]').forEach(function (el) { el.textContent = T(el.getAttribute('data-i18n')); });
      $('q').placeholder = T('ask.placeholder');
      renderLangs(); renderList(); renderFacts(); renderProgress(lastStatus);
      if (!first && state.tab === 'report' && $('report').innerHTML) genReport();
    });
  }
  function renderLangs() {
    $('langs').innerHTML = LANGS.map(function (l) {
      return '<button class="' + (l === state.lang ? 'on' : '') + '" data-l="' + l + '">' + l.toUpperCase() + '</button>'; }).join('');
    $('langs').querySelectorAll('button').forEach(function (b) { b.onclick = function () { setLang(b.getAttribute('data-l')); }; });
  }

  // --- liste et fiche ------------------------------------------------------
  function loadPapers() {
    return fetch('/api/papers').then(function (r) { return r.json(); }).then(function (ps) {
      var before = JSON.stringify(state.papers.filter(function (p) { return p.id === state.selected; })[0] || null);
      state.papers = ps;
      if (state.selected && !ps.some(function (p) { return p.id === state.selected; })) state.selected = null;
      var after = JSON.stringify(ps.filter(function (p) { return p.id === state.selected; })[0] || null);
      renderList(); loadMonths();
      if (before !== after || !state.selected) renderFacts();  // la fiche ne bouge que si son papier a changé
    });
  }
  function renderList() {
    var el = $('list');
    if (!state.papers.length) { el.innerHTML = '<div class="empty">' + esc(T('papers.empty')) + '</div>'; return; }
    el.innerHTML = state.papers.map(function (p) {
      return '<button class="row' + (p.id === state.selected ? ' on' : '') + '" data-id="' + esc(p.id) + '">' +
        '<span class="badge">' + esc(T('kind.' + p.kind)) + '</span>' +
        '<span class="f">' + esc(p.id) + ' · ' + esc(p.file) + '</span>' +
        '<span class="badge lang">' + esc(p.language) + '</span></button>'; }).join('');
    el.querySelectorAll('.row').forEach(function (b) { b.onclick = function () { state.selected = b.getAttribute('data-id'); renderList(); renderFacts(); }; });
  }
  function glyph(st) { return st === 'verified' ? '✓' : (st === 'model' ? '•' : '—'); }
  function fieldRow(key, f) {
    var st = f ? f.status : 'blank', v = f ? f.value : null, reason = f ? f.reason : 'missing';
    var html = '<tr><th>' + esc(T('field.' + key)) + '</th><td><span class="g ' + st + '" title="' + esc(T('status.' + st)) + '">' + glyph(st) + '</span></td>';
    if (v == null) {
      html += '<td class="v blank">' + esc(T('status.blank')) + (reason ? ' <span class="reason">· ' + esc(T('status.reason')) + ' : ' + esc(reason) + '</span>' : '') + '</td>';
    } else if (key === 'movements' && Array.isArray(v)) {
      html += '<td class="v"><table class="mov">' + v.map(function (m) {
        return '<tr><td>' + esc(m.date || '') + '</td><td>' + esc(m.label || '') + '</td><td class="num">' + chf(m.amount) + '</td></tr>'; }).join('') + '</table>';
      if (reason) html += '<span class="reason">' + esc(reason) + '</span>';
      html += '</td>';
    } else {
      var shown = (typeof v === 'number') ? chf(v) : String(v);
      html += '<td class="v">' + esc(shown) + (st === 'model' && reason ? ' <span class="reason">· ' + esc(reason) + '</span>' : '') +
        (st === 'verified' && f.source ? ' <span class="reason">· ' + esc(f.source) + '</span>' : '') + '</td>';
    }
    return html + '</tr>';
  }
  function renderFacts() {
    var el = $('facts');
    var p = state.papers.filter(function (x) { return x.id === state.selected; })[0];
    if (!p) { el.innerHTML = '<div class="empty">' + esc(state.papers.length ? T('paper.select') : T('papers.empty')) + '</div>'; return; }
    var keys = FIELD_ORDER[p.kind] || Object.keys(p.facts || {});
    var rows = keys.map(function (k) { return fieldRow(k, (p.facts || {})[k]); }).join('');
    el.innerHTML = '<h2>' + esc(p.id) + ' · ' + esc(T('kind.' + p.kind)) + '</h2>' +
      '<div class="meta"><span>' + esc(p.file) + '</span><span>' + esc(p.language) + ' · ' + p.pages + ' ' + esc(T('paper.pages')) + '</span>' +
      '<a class="btn" href="/api/paper/' + esc(p.id) + '/file" target="_blank">' + esc(T('paper.open')) + '</a></div>' +
      '<table>' + rows + '</table>' +
      '<div class="legend"><span><span class="g verified">✓</span> ' + esc(T('status.verified')) + '</span>' +
      '<span><span class="g model">•</span> ' + esc(T('status.model')) + '</span>' +
      '<span><span class="g blank">—</span> ' + esc(T('status.blank')) + '</span></div>' +
      (p.qr_payload ? '<details><summary>' + esc(T('paper.qr')) + '</summary><pre>' + esc(p.qr_payload) + '</pre></details>' : '') +
      '<details><summary>' + esc(T('paper.text')) + '</summary><pre>' + esc(p.text) + '</pre></details>';
  }

  // --- dépôt ---------------------------------------------------------------
  var lastStatus = null, pollTimer = null;
  function renderProgress(s) {
    var el = $('progress');
    if (!s) { el.textContent = ''; return; }
    var txt = '';
    if (s.running) txt = T('drop.reading') + ' ' + s.done + ' / ' + s.total + (s.current ? ' · ' + s.current : '');
    else if (s.total) txt = T('drop.done') + ' · ' + s.papers + ' ' + T('papers.count');
    if (s.errors && s.errors.length) txt += ' <span class="err">' + s.errors.length + ' × ' + esc(T('error')) + ' (' + esc(s.errors[s.errors.length - 1].error) + ')</span>';
    el.innerHTML = txt;
  }
  function poll() {
    fetch('/status').then(function (r) { return r.json(); }).then(function (s) {
      var before = lastStatus ? lastStatus.papers : -1;
      lastStatus = s; renderProgress(s);
      if (s.papers !== before) loadPapers();
      clearTimeout(pollTimer);
      pollTimer = setTimeout(poll, s.running ? 2000 : 15000);
    }).catch(function () { pollTimer = setTimeout(poll, 5000); });
  }
  function upload(files) {
    files = Array.prototype.filter.call(files, function (f) { return /\.pdf$/i.test(f.name); });
    if (!files.length) { $('progress').textContent = T('drop.nothing'); return; }
    var i = 0, batch = 10;
    function next() {
      if (i >= files.length) { poll(); return; }
      var fd = new FormData();
      files.slice(i, i + batch).forEach(function (f) { fd.append('files', f, f.name); });
      i += batch;
      fetch('/papers', { method: 'POST', body: fd }).then(function () { $('progress').textContent = T('drop.uploaded') + ' : ' + Math.min(i, files.length) + ' / ' + files.length; next(); });
    }
    next();
  }
  $('fdir').onchange = function () { upload(this.files); this.value = ''; };
  $('ffiles').onchange = function () { upload(this.files); this.value = ''; };
  var drop = $('drop');
  ['dragenter', 'dragover'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); }); });
  ['dragleave', 'drop'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); }); });
  drop.addEventListener('drop', function (e) {
    var items = e.dataTransfer.items, out = [], pending = 0;
    function walk(entry) {
      if (entry.isFile) { pending++; entry.file(function (f) { out.push(f); if (--pending === 0) upload(out); }); }
      else if (entry.isDirectory) { pending++; entry.createReader().readEntries(function (es) { es.forEach(walk); if (--pending === 0) upload(out); }); }
    }
    if (items && items.length && items[0].webkitGetAsEntry) {
      for (var i = 0; i < items.length; i++) { var en = items[i].webkitGetAsEntry(); if (en) walk(en); }
      if (!pending) upload(e.dataTransfer.files);
    } else upload(e.dataTransfer.files);
  });

  // --- onglets ------------------------------------------------------------
  function showTab(t) {
    state.tab = t;
    ['papers', 'report'].forEach(function (x) {
      $('tab-' + x).classList.toggle('on', x === t); $('panel-' + x).classList.toggle('on', x === t); });
  }
  $('tab-papers').onclick = function () { showTab('papers'); };
  $('tab-report').onclick = function () { showTab('report'); if (!$('report').innerHTML) genReport(); };

  // --- rapport ------------------------------------------------------------
  function loadMonths() {
    fetch('/api/months').then(function (r) { return r.json(); }).then(function (ms) {
      var sel = $('month'), cur = sel.value;
      if (!ms.length) ms = ['2026-09'];
      sel.innerHTML = ms.map(function (m) { return '<option value="' + m + '">' + m + '</option>'; }).join('');
      sel.value = (ms.indexOf(cur) >= 0) ? cur : (ms.indexOf('2026-09') >= 0 ? '2026-09' : ms[0]);
      $('md').href = '/api/report.md?month=' + sel.value + '&lang=' + state.lang;
    });
  }
  function genReport() {
    var m = $('month').value || '2026-09';
    $('md').href = '/api/report.md?month=' + m + '&lang=' + state.lang;
    $('gen').disabled = true; $('report').innerHTML = '<p class="muted">' + esc(T('loading')) + '</p>';
    fetch('/api/report?month=' + m + '&lang=' + state.lang).then(function (r) { return r.json(); }).then(function (d) {
      $('report').innerHTML = d.html; $('gen').disabled = false;
    }).catch(function () { $('report').innerHTML = '<p class="muted">' + esc(T('error')) + '</p>'; $('gen').disabled = false; });
  }
  $('gen').onclick = genReport;
  $('month').onchange = function () { $('md').href = '/api/report.md?month=' + this.value + '&lang=' + state.lang; };

  // --- question -----------------------------------------------------------
  $('askform').onsubmit = function (e) {
    e.preventDefault();
    var q = $('q').value.trim(); if (!q) return;
    var out = $('out'); out.className = 'out on'; out.innerHTML = '<span class="muted">' + esc(T('loading')) + '</span>';
    $('askbtn').disabled = true;
    fetch('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q, lang: state.lang }) })
      .then(function (r) { return r.json(); }).then(function (a) {
        var text = esc(a.text).replace(/\[(p\d{3,})\]/g, function (_, id) { return '<a class="pid" href="/api/paper/' + id + '/file" target="_blank">[' + id + ']</a>'; });
        var cites = (a.papers || []).map(function (p) { return '<a class="pid" href="/api/paper/' + esc(p.id) + '/file" target="_blank">' + esc(p.id) + '</a> <span class="muted">' + esc(T('kind.' + p.kind)) + '</span>'; }).join(' · ');
        out.innerHTML = '<div>' + text + '</div>' +
          (cites ? '<div class="st">' + esc(T('ask.sources')) + ' : ' + cites + '</div>' : '') +
          '<div class="st">' + (a.grounded ? '<span class="ok">✓ ' + esc(T('ask.grounded')) + '</span>' : esc(T('ask.not_grounded'))) +
          (a.by === 'code' ? ' · ' + esc(T('ask.by_code')) : (a.by === 'model' ? ' · ' + esc(T('ask.by_model')) : '')) +
          (a.note ? ' · ' + esc(a.note) : '') + '</div>';
        $('askbtn').disabled = false;
      }).catch(function () { out.innerHTML = '<span class="muted">' + esc(T('error')) + '</span>'; $('askbtn').disabled = false; });
  };

  // --- départ ---------------------------------------------------------------
  // L'adresse peut préciser ?lang=de&paper=p050&tab=report (lien partageable, captures d'écran).
  var params = new URLSearchParams(location.search);
  var saved = 'fr'; try { saved = localStorage.getItem('paperasse.lang') || 'fr'; } catch (e) {}
  if (params.get('lang')) saved = params.get('lang');
  if (LANGS.indexOf(saved) < 0) saved = 'fr';
  if (params.get('paper')) state.selected = params.get('paper');
  setLang(saved, true);
  loadPapers().then(function () { if (params.get('tab') === 'report') { showTab('report'); genReport(); } });
  poll();
})();
</script>
</body>
</html>
"""
