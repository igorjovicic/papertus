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

from datetime import date

from . import agenda as agenda_mod
from . import ask as ask_mod
from . import i18n
from . import report as report_mod
from .schema import Paper

PKG_ROOT = Path(__file__).resolve().parents[2]  # track_2b/
PAPERS_DIR = Path(os.getenv("PAPERS_DIR") or "./papers").resolve()
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR") or (PKG_ROOT / "runtime")).resolve()
CACHE_FILE = RUNTIME_DIR / "cache.json"
def _state_file() -> Path:
    """L'état « fait / payé » des événements de l'agenda, à côté du cache (lu à l'appel : les tests
    déplacent RUNTIME_DIR)."""
    return RUNTIME_DIR / "state.json"
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
        self.cache: dict[str, dict[str, Any]] = {}  # clé d'exécution → Paper.to_dict() (+ read_with)
        self.seed: dict[str, dict[str, Any]] = {}   # clé de graine → Paper.to_dict() (+ read_with), jamais réécrite
        self.read_with: dict[str, dict[str, Any]] = {}  # id → {model, size, local}
        self.done: set[str] = set()                     # ids d'événements marqués faits / payés
        self.queue: "queue.Queue[Path | None]" = queue.Queue()
        self.status: dict[str, Any] = {"total": 0, "done": 0, "running": False, "current": None, "errors": []}
        self.index: ask_mod.Index | None = None
        self.thread: threading.Thread | None = None

    # --- cache ---------------------------------------------------------------
    def load_cache(self) -> None:
        """Le cache d'exécution (runtime/) et la graine livrée (data/cache.json), lue mais jamais écrite."""
        self.cache, self.seed = {}, {}
        for path, target in ((CACHE_FILE, self.cache), (SEED_CACHE, self.seed)):
            try:
                got = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(got, dict):
                    target.update(got)
            except (OSError, ValueError):
                continue

    def load_state(self) -> None:
        try:
            got = json.loads(_state_file().read_text(encoding="utf-8"))
            self.done = {str(x) for x in got.get("done", [])}
        except (OSError, ValueError, AttributeError):
            self.done = set()

    def save_state(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"done": sorted(self.done)}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_state_file())

    def set_done(self, event_id: str, done: bool) -> None:
        with self.lock:
            if done:
                self.done.add(event_id)
            else:
                self.done.discard(event_id)
            self.save_state()

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
    def add_paper(self, paper: Paper, path: Path, digest: str, read_with: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.papers[paper.id] = paper
            self.paths[paper.id] = path
            self.hashes[paper.id] = digest
            self.read_with[paper.id] = dict(read_with or {})
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
        cached = self.cache.get(cache_key(digest)) or self.seed.get(seed_key(digest)) or self.from_eval_cache(path, digest)
        if cached:
            paper = Paper.from_dict(cached)
            with self.lock:
                if paper.id in self.papers:
                    paper.id = self.new_id(path)
            self.add_paper(paper, path, digest, cached.get("read_with") or read_with_now())
            with self.lock:
                if cache_key(digest) not in self.cache:
                    self.cache[cache_key(digest)] = dict(paper.to_dict(), read_with=self.read_with[paper.id])
                    self.save_cache()
            return
        from . import read  # import tardif : pymupdf/zxing seulement quand on lit vraiment
        with self.lock:
            pid = self.new_id(path)
        paper = read.read_paper(str(path), paper_id=pid, rel_file=path.name)
        paper.file = path.name
        self.add_paper(paper, path, digest, read_with_now())
        with self.lock:
            self.cache[cache_key(digest)] = dict(paper.to_dict(), read_with=self.read_with[paper.id])
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
            self.thread = threading.Thread(target=self.worker, name="papertus-reader", daemon=True)
            self.thread.start()


STORE = Store()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def reader_hash() -> str:
    """Empreinte du code de lecture (read.py + verify.py + schema.py) : une lecture en cache ne vaut
    que pour le code qui l'a produite. Ne dépend PAS du modèle."""
    h = hashlib.sha256()
    for name in ("read.py", "verify.py", "schema.py"):
        try:
            h.update((Path(__file__).parent / name).read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def reader_stamp() -> str:
    """Empreinte du code de lecture ET du nom du modèle (clé du cache d'exécution)."""
    h = hashlib.sha256((reader_hash() + "|" + (os.getenv("LLM_NAME") or "")).encode())
    return h.hexdigest()[:12]


def seed_key(digest: str) -> str:
    """Clé de la graine livrée (data/cache.json) : fichier + code de lecture, quel que soit le modèle."""
    return f"{digest}:{reader_hash()}"


def read_with_now() -> dict[str, Any]:
    """Avec quoi une lecture faite maintenant est faite : nom du modèle, taille, local ou cloud."""
    d = report_mod.deployment()
    return {"model": os.getenv("LLM_NAME") or "", "size": d["size"], "local": d["local"]}


def cache_key(digest: str) -> str:
    """Clé du cache d'exécution : fichier + code de lecture + modèle."""
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
    STORE.load_state()
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


app = FastAPI(title=i18n.APP_NAME, lifespan=_lifespan)


def paper_json(p: Paper, *, full: bool = False) -> dict[str, Any]:
    d = p.to_dict()
    if not full and len(d.get("text", "")) > TEXT_PREVIEW:
        d["text"] = d["text"][:TEXT_PREVIEW] + " …"
    d["file"] = STORE.paths.get(p.id, Path(p.file)).name
    d["read_with"] = STORE.read_with.get(p.id, {})
    return d


@app.get("/health")
def health() -> dict[str, Any]:
    from . import llm  # compteur de jetons du processus, pour la transparence
    with STORE.lock:
        return {"ok": True, "papers": len(STORE.papers), "reading": STORE.status["running"],
                "model": os.getenv("LLM_NAME") or "", "endpoint_set": bool(os.getenv("LLM_BASE_URL")),
                "deployment": report_mod.deployment(), "model_usage": llm.usage_total()}


@app.get("/status")
def status() -> dict[str, Any]:
    with STORE.lock:
        s = dict(STORE.status)
        s["papers"] = len(STORE.papers)
        s["errors"] = s["errors"][-10:]
        return s


@app.get("/api/strings")
def strings(lang: str = "fr") -> dict[str, str]:
    out = i18n.all_strings(lang)
    out["_status"] = report_mod.status_line(lang)
    out["_badge"] = report_mod.model_badge(lang)
    out["_app"] = i18n.APP_NAME
    return out


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
                             headers={"Content-Disposition": f'attachment; filename="papertus-{month}-{lang}.md"'})


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


def _today(today: str | None) -> date:
    """`?today=YYYY-MM-DD` fixe la date du jour (captures, tests) ; sinon la vraie."""
    if today:
        d = report_mod.as_date(today)
        if d is None:
            raise HTTPException(400, "today must be YYYY-MM-DD")
        return d
    return date.today()


@app.get("/api/agenda")
def api_agenda(today: str | None = None, lang: str = "fr", month: str | None = None) -> dict[str, Any]:
    if month and not _MONTH_RE.match(month):
        raise HTTPException(400, "month must be YYYY-MM")
    with STORE.lock:
        done = set(STORE.done)
    return agenda_mod.build_agenda(STORE.ordered(), _today(today), done, i18n.norm_lang(lang), month)


@app.post("/api/agenda/done")
async def api_agenda_done(request: Request) -> dict[str, Any]:
    body = await request.json()
    event_id = str(body.get("event_id") or "").strip()
    if not re.fullmatch(r"p\d{3,}:(bill|deadline|statement)", event_id):
        raise HTTPException(400, "event_id must look like p001:bill")
    done = bool(body.get("done", True))
    STORE.set_done(event_id, done)
    return {"event_id": event_id, "done": done}


@app.get("/api/agenda.ics")
def api_agenda_ics(lang: str = "fr", today: str | None = None) -> PlainTextResponse:
    with STORE.lock:
        done = set(STORE.done)
    events = agenda_mod.events_from_papers(STORE.ordered(), done)
    agenda_mod.group_events(events, _today(today))
    text = agenda_mod.to_ics(events, i18n.norm_lang(lang))
    return PlainTextResponse(text, media_type="text/calendar; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{i18n.APP_NAME.lower()}-agenda.ics"'})


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
    return HTMLResponse(PAGE.replace("{{APP}}", i18n.APP_NAME))


# ---------------------------------------------------------------------------
# La page : tout en ligne, noir sur blanc, un seul accent (#d52b1e), gris pour les blancs
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{APP}}</title>
<style>
/* Style international suisse, avec le soin du détail : une échelle d'espacement (8/16/24/32/48),
   un seul filet entre deux choses, jamais deux ; noir sur blanc, un seul rouge. */
:root { --ink:#111; --paper:#fff; --grey:#6b6b6b; --rule:#111; --hair:#dcdcdc; --soft:#f4f4f4; --red:#d52b1e;
  --s1:8px; --s2:16px; --s3:24px; --s4:32px; --s6:48px; }
* { box-sizing:border-box; }
html, body { margin:0; background:var(--paper); color:var(--ink);
  font-family:"Helvetica Neue", Helvetica, Arial, -apple-system, BlinkMacSystemFont, sans-serif; font-size:15px; line-height:1.5;
  -webkit-font-smoothing:antialiased; }
body { padding-bottom:160px; }  /* remplacé au chargement par la hauteur réelle de la barre de question */
a { color:var(--ink); }
a.pid { color:var(--ink); text-decoration:none; border-bottom:1px solid var(--ink); font-variant-numeric:tabular-nums; }
.label { display:block; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); font-weight:400; line-height:1.4; }
.num, .amt { font-variant-numeric:tabular-nums; }
:focus-visible { outline:2px solid var(--ink); outline-offset:2px; }
[hidden] { display:none !important; }

/* la grille : 12 colonnes, gouttières de 24 px, marges de 32 px */
.wrap { max-width:1280px; margin:0 auto; padding:0 var(--s4); }
.grid { display:grid; grid-template-columns:repeat(12, minmax(0, 1fr)); column-gap:var(--s3); }

/* en-tête : marque, slogan, langues ; dessous la capsule d'état ; un seul filet sous l'ensemble */
header { border-bottom:1px solid var(--rule); }
header .grid { align-items:center; min-height:72px; }
.mark { grid-column:1 / span 3; font-size:24px; font-weight:700; letter-spacing:-.02em; margin:0; line-height:1; }
.mark b { color:var(--red); }
.tag { grid-column:4 / span 5; color:var(--grey); font-size:14px; }
.langs { grid-column:9 / span 4; display:flex; justify-content:flex-end; }
.langs .sw { display:inline-flex; border:1px solid var(--ink); }
.langs button { border:none; border-left:1px solid var(--ink); min-width:40px; min-height:40px; padding:0 12px; font-size:13px; letter-spacing:.04em; }
.langs button:first-child { border-left:none; }
.langs button.on { background:var(--ink); color:var(--paper); }
.pill { display:inline-flex; align-items:center; gap:var(--s1); margin:0 0 var(--s2); padding:0 12px; min-height:28px;
  border:1px solid var(--hair); border-radius:999px; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--ink); }
.pill i { width:6px; height:6px; background:var(--ink); border-radius:50%; display:inline-block; }

button, .btn { min-height:40px; min-width:40px; padding:0 var(--s2); border:1px solid var(--ink); background:var(--paper); color:var(--ink);
  font:inherit; font-size:14px; line-height:1; border-radius:0; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; text-decoration:none; }
button.on, .btn.on { background:var(--ink); color:var(--paper); }
button.primary, .btn.primary { background:var(--red); border-color:var(--red); color:#fff; font-weight:700; }
button:disabled { opacity:.4; cursor:default; }

main.wrap { padding-top:var(--s3); }

/* la zone de dépôt : un cadre, 24 px dedans, les boutons alignés au milieu */
.drop { grid-column:1 / span 12; border:1px solid var(--ink); padding:var(--s3); display:grid; grid-template-columns:minmax(0,1fr) auto; gap:var(--s2) var(--s3); align-items:center; }
.drop.over { background:var(--soft); }
.drop strong { display:block; font-size:17px; font-weight:700; letter-spacing:-.01em; line-height:1.35; }
.drop span.hint { display:block; color:var(--grey); font-size:14px; margin-top:4px; }
.drop .acts { display:flex; gap:var(--s1); flex-wrap:wrap; }
input[type=file] { display:none; }
.empty-line { grid-column:1 / span 8; font-size:22px; line-height:1.35; letter-spacing:-.01em; margin:0 0 var(--s3); }
.progress { grid-column:1 / span 12; font-size:13px; color:var(--grey); min-height:20px; margin-top:var(--s1); font-variant-numeric:tabular-nums; }
.progress .err { color:var(--red); }

/* les onglets : un seul filet ; le trait de l'onglet actif est posé SUR ce filet */
.tabs { grid-column:1 / span 12; display:flex; gap:var(--s4); margin:var(--s6) 0 var(--s4); border-bottom:1px solid var(--rule); }
.tabs button { border:none; border-bottom:2px solid transparent; margin-bottom:-1px; padding:0 0 12px; min-height:44px; font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--grey); background:none; }
.tabs button.on { color:var(--ink); border-bottom-color:var(--ink); background:none; }
.panel { display:none; grid-column:1 / span 12; } .panel.on { display:grid; grid-template-columns:repeat(12, minmax(0, 1fr)); column-gap:var(--s3); }

/* agenda */
.agenda p.today { grid-column:1 / span 10; font-size:24px; line-height:1.3; letter-spacing:-.015em; margin:0 0 var(--s4); }
.agenda p.today b { font-weight:400; color:var(--grey); display:block; font-size:11px; letter-spacing:.1em; text-transform:uppercase; margin-bottom:var(--s1); line-height:1.4; }
.cal { grid-column:1 / span 12; border:1px solid var(--ink); margin-bottom:var(--s3); background:var(--paper); }
.cal-head { background:var(--red); color:#fff; display:flex; align-items:center; gap:var(--s1); padding:var(--s1) var(--s2); min-height:56px; }
.cal-head h2 { flex:1; margin:0; font-size:20px; font-weight:700; letter-spacing:-.01em; color:#fff; line-height:1.2; }
.cal-head button { border:1px solid #fff; color:#fff; background:transparent; min-width:40px; min-height:40px; padding:0 12px; font-size:18px; line-height:1; }
.cal-head button.today { font-size:13px; padding:0 var(--s2); }
.cal-head button:hover { background:rgba(255,255,255,.14); }
.cal-head button:focus-visible { outline-color:#fff; }
.cal-days { display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); border-bottom:1px solid var(--ink); }
.cal-days span { padding:var(--s1) 12px; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); }
.cal-grid { display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); }
.cell { min-height:96px; padding:var(--s1); border-right:1px solid var(--hair); border-bottom:1px solid var(--hair); display:flex; flex-direction:column; align-items:stretch; gap:3px; cursor:pointer; background:var(--paper); min-width:0; overflow:hidden; text-align:left; font:inherit; color:inherit; }
.cell:nth-child(7n) { border-right:none; }
.cal-grid .cell:nth-last-child(-n+7) { border-bottom:none; }
.cell.we { background:#fafafa; }
.cell.out { color:#b5b5b5; }
.cell.out .chip { opacity:.55; }
.cell.sel { box-shadow:inset 0 0 0 2px var(--ink); }
.cell .dn { font-size:16px; font-weight:700; line-height:1; font-variant-numeric:tabular-nums; align-self:flex-start; }
.cell.today .dn { display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; margin:-4px 0 0 -4px; border-radius:50%; background:var(--red); color:#fff; }
.chip { display:block; width:100%; max-width:100%; font-size:12px; line-height:18px; padding:0 6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-left:2px solid transparent; min-width:0; text-align:left; }
.chip.bill { background:#f3f3f3; color:var(--ink); }
.chip.deadline { background:var(--ink); color:#fff; }
.chip.statement { border:1px solid #b5b5b5; border-left:2px solid transparent; color:var(--grey); line-height:16px; }
.chip.late { border-left-color:var(--red); }
.chip.done { text-decoration:line-through; opacity:.5; }
.chip.more { color:var(--grey); padding-left:0; }
.cell .sum { margin-top:auto; align-self:flex-end; font-size:12px; font-variant-numeric:tabular-nums; color:var(--ink); }
.cell .dots { display:none; }
.wk-title { grid-column:1 / span 12; margin-bottom:var(--s1); }
.wk { grid-column:1 / span 12; display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); border:1px solid var(--ink); margin-bottom:var(--s3); }
.wk .wd { padding:12px var(--s2) var(--s2); border-left:1px solid var(--hair); min-width:0; }
.wk .wd:first-child { border-left:none; }
.wk .wd .m { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); }
.wk .wd .n { font-size:28px; font-weight:700; letter-spacing:-.03em; line-height:1.1; font-variant-numeric:tabular-nums; margin:2px 0 6px; }
.wk .wd.today .n { color:var(--red); }
.wk .wd .c { font-size:12px; color:var(--grey); font-variant-numeric:tabular-nums; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.wk .wd .c b { color:var(--ink); font-weight:600; }
.events .dayfilter { display:flex; align-items:center; gap:var(--s2); margin:var(--s3) 0 0; padding-bottom:12px; border-bottom:1px solid var(--rule); font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--grey); }
.events .dayfilter a { color:var(--ink); }
.figs { grid-column:1 / span 12; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); border:1px solid var(--ink); padding:var(--s2) 0; margin-bottom:var(--s3); }
.fig { position:relative; padding:var(--s1) var(--s3); }
.fig + .fig::before { content:""; position:absolute; left:0; top:0; bottom:0; width:1px; background:var(--hair); }
.fig .n { font-size:40px; font-weight:700; letter-spacing:-.03em; line-height:1; font-variant-numeric:tabular-nums; }
.fig.red .n { color:var(--red); }
.fig .u { font-size:11px; color:var(--grey); letter-spacing:.06em; text-transform:uppercase; margin-top:var(--s1); line-height:1.4; }
.events { grid-column:1 / span 12; }
.events h3 { font-size:11px; letter-spacing:.1em; text-transform:uppercase; font-weight:400; color:var(--grey); margin:var(--s3) 0 0; padding-bottom:12px; border-bottom:1px solid var(--rule); }
.events h3.red { color:var(--red); }
.ev { display:grid; grid-template-columns:88px 96px minmax(0,1fr) 140px 96px 56px; column-gap:var(--s2); align-items:center; padding:var(--s2) 0; }
.ev + .ev { border-top:1px solid var(--hair); }
.ev .d { font-size:20px; font-weight:700; letter-spacing:-.02em; font-variant-numeric:tabular-nums; white-space:nowrap; line-height:1.1; }
.ev .d small { display:block; font-size:11px; font-weight:400; color:var(--grey); letter-spacing:.06em; margin-top:2px; }
.ev .k { font-size:11px; letter-spacing:.1em; color:var(--grey); min-width:0; overflow-wrap:anywhere; line-height:1.5; }
.ev .k small { display:block; letter-spacing:.02em; text-transform:none; }
.ev .w { min-width:0; }
.ev .w .who { font-weight:600; line-height:1.3; }
.ev .w .what { color:var(--grey); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:2px; }
.ev .w .why { color:var(--grey); font-size:12px; margin-top:4px; line-height:1.4; }
.ev .a { text-align:right; font-variant-numeric:tabular-nums; font-size:17px; white-space:nowrap; }
.ev .a.blank { color:var(--grey); font-size:12px; letter-spacing:.04em; }
.ev .a .st { display:block; font-size:11px; color:var(--grey); letter-spacing:.06em; margin-top:2px; }
.ev button { width:96px; padding:0; font-size:13px; }
.ev button.on { background:var(--ink); color:var(--paper); }
.ev.done .d, .ev.done .k, .ev.done .w, .ev.done .a { color:var(--grey); }
.ev .p { text-align:right; }
.reminders { grid-column:1 / span 12; display:flex; flex-wrap:wrap; gap:12px var(--s3); align-items:center; margin-top:var(--s6); padding-top:var(--s3); border-top:1px solid var(--rule); }
.reminders .hint { color:var(--grey); font-size:13px; flex:1 1 320px; }

/* liste des papiers et fiche */
.list { grid-column:1 / span 4; border-top:1px solid var(--rule); max-height:72vh; overflow:auto; }
.list .row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:2px 12px; width:100%; min-height:56px; padding:12px 0;
  border:none; background:none; text-align:left; cursor:pointer; font:inherit; border-radius:0; }
.list .row + .row { border-top:1px solid var(--hair); }
.list .row.on { background:var(--soft); box-shadow:inset 3px 0 0 var(--ink); padding-left:12px; }
.list .row .k { grid-column:1; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); }
.list .row .f { grid-column:1; min-width:0; font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.list .row .l { grid-column:2; grid-row:1 / span 2; align-self:center; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); }
.list .empty, .facts .empty { padding:var(--s3) 0; color:var(--grey); }
.facts { grid-column:5 / span 8; }
.facts h2 { font-size:26px; font-weight:700; letter-spacing:-.02em; margin:0 0 var(--s1); line-height:1.1; }
.facts .meta { color:var(--grey); font-size:13px; margin-bottom:var(--s1); display:flex; flex-wrap:wrap; gap:var(--s1) var(--s3); align-items:center; }
.facts .readwith { color:var(--grey); font-size:12px; margin:0 0 var(--s3); }
.facts > table { table-layout:fixed; }
.facts col.c1 { width:200px; } .facts col.c2 { width:40px; }
.facts th { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); font-weight:400; padding-top:15px; }
.facts td.v { word-break:break-word; min-width:0; }
.facts td.v.amt { font-size:28px; font-weight:700; letter-spacing:-.02em; line-height:1.1; }
.facts td.blank { color:var(--grey); }
.facts .reason { display:block; color:var(--grey); font-size:12px; font-weight:400; letter-spacing:0; margin-top:4px; line-height:1.4; }
table { border-collapse:collapse; width:100%; font-size:15px; }
th, td { text-align:left; padding:12px var(--s2) 12px 0; border-bottom:1px solid var(--hair); vertical-align:top; }
thead th { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--grey); font-weight:400; border-bottom:1px solid var(--rule); }
td.num, th.num { text-align:right; white-space:nowrap; }
tr.total td { font-weight:700; border-top:1px solid var(--rule); border-bottom:none; }
tr.warn td { color:var(--red); font-size:13px; }
.g { display:inline-block; width:24px; font-weight:700; }
.g.verified { color:var(--red); }
.g.model { color:var(--ink); }
.g.blank { color:var(--grey); }
.muted { color:var(--grey); }
.legend { font-size:12px; color:var(--grey); display:flex; gap:var(--s3); flex-wrap:wrap; margin-top:var(--s2); }
.mov { margin:4px 0; font-size:13px; table-layout:auto; }
.mov td { padding:4px var(--s1) 4px 0; border-bottom:1px solid var(--hair); word-break:normal; }
.mov tr:last-child td { border-bottom:none; }
.mov td.num { width:1%; white-space:nowrap; }
.mov td:nth-child(2) { width:98%; }
details { margin-top:var(--s2); } details summary { cursor:pointer; color:var(--grey); font-size:12px; letter-spacing:.08em; text-transform:uppercase; }
pre { white-space:pre-wrap; font-size:12px; background:var(--soft); padding:12px; max-height:320px; overflow:auto; font-family:"SF Mono", Menlo, Consolas, monospace; }

/* rapport */
.bar { grid-column:1 / span 12; display:flex; flex-wrap:wrap; gap:var(--s1) var(--s2); align-items:center; margin-bottom:var(--s3); }
select { min-height:40px; font:inherit; padding:0 12px; border:1px solid var(--ink); border-radius:0; background:var(--paper); }
.report #report { grid-column:1 / span 12; overflow-x:auto; }
.report td.d, .report td.num { white-space:nowrap; }
.report h2 { font-size:26px; font-weight:700; letter-spacing:-.02em; margin:0 0 var(--s3); line-height:1.1; }
.report h3 { font-size:11px; letter-spacing:.1em; text-transform:uppercase; font-weight:400; color:var(--grey); margin:var(--s4) 0 var(--s1); }
.report .prose { border-left:3px solid var(--red); padding:4px 0 4px 20px; margin:var(--s1) 0 0; max-width:720px; }
.report .prose p { margin:0 0 var(--s1); font-size:17px; line-height:1.5; }

/* la barre de question : fixe, un filet dessus, fond blanc, jamais par-dessus le contenu */
.ask { position:fixed; left:0; right:0; bottom:0; background:var(--paper); border-top:1px solid var(--rule); padding:12px 0 var(--s2); }
.ask .chips { display:flex; flex-wrap:nowrap; gap:var(--s1); overflow-x:auto; padding-bottom:12px; align-items:center; -webkit-overflow-scrolling:touch; }
.ask .chips .label { white-space:nowrap; margin-right:var(--s1); flex:0 0 auto; }
.ask .chips button { flex:0 0 auto; white-space:nowrap; font-size:13px; color:var(--ink); border-color:var(--hair); }
.ask .chips button:hover { border-color:var(--ink); }
.ask .in { display:flex; }
.ask input { flex:1; min-width:0; min-height:44px; font:inherit; padding:0 12px; border:1px solid var(--ink); border-right:none; border-radius:0; }
.ask .in button { min-height:44px; }
.ask .out { margin:12px 0 0; font-size:15px; display:none; max-width:900px; }
.ask .out.on { display:block; }
.ask .out .st { font-size:12px; color:var(--grey); margin-top:4px; }
.ask .out .st .ok { color:var(--red); }

@media (max-width:760px) {
  .wrap { padding:0 var(--s2); }
  header .grid { min-height:auto; padding:12px 0 0; row-gap:12px; }
  .mark { grid-column:1 / span 12; font-size:22px; }
  .tag { display:none; }
  .langs { grid-column:1 / span 12; justify-content:flex-start; }
  .pill { margin:12px 0 var(--s2); }
  main.wrap { padding-top:var(--s2); }
  .drop { grid-template-columns:1fr; padding:var(--s2); }
  .drop strong { font-size:16px; }
  .empty-line { grid-column:1 / span 12; font-size:18px; }
  .tabs { margin:var(--s4) 0 var(--s3); gap:var(--s3); }
  .list, .facts, .bar, .report #report { grid-column:1 / span 12; }
  .list { max-height:36vh; margin-bottom:var(--s3); }
  .facts col.c1 { width:120px; }
  .facts td.v.amt { font-size:22px; }
  .agenda p.today { grid-column:1 / span 12; font-size:19px; }
  .cal-head { padding:var(--s1) 12px; } .cal-head h2 { font-size:17px; }
  .cal-days span { padding:6px 4px; font-size:10px; letter-spacing:.04em; text-align:center; }
  .cell { min-height:52px; padding:4px; gap:4px; align-items:center; }
  .cell .dn { font-size:14px; align-self:center; }
  .cell.today .dn { width:24px; height:24px; margin:0; }
  .chip, .cell .sum { display:none; }
  .cell .dots { display:flex; gap:3px; justify-content:center; flex-wrap:wrap; }
  .dot { width:6px; height:6px; border-radius:50%; }
  .dot.bill { background:#b5b5b5; } .dot.deadline { background:var(--ink); } .dot.statement { border:1px solid #b5b5b5; }
  .dot.late { background:var(--red); border-color:var(--red); }
  .wk { display:flex; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .wk .wd { flex:0 0 112px; }
  .figs { grid-template-columns:repeat(2, minmax(0,1fr)); padding:var(--s2) 0; }
  .fig { padding:var(--s1) var(--s2); }
  .fig:nth-child(3)::before { display:none; }
  .fig:nth-child(n+3) { margin-top:var(--s2); padding-top:var(--s2); }
  .fig:nth-child(n+3)::after { content:""; position:absolute; left:var(--s2); right:var(--s2); top:0; height:1px; background:var(--hair); }
  .fig:nth-child(4)::before { top:var(--s2); }
  .fig .n { font-size:28px; }
  .ev { grid-template-columns:72px minmax(0,1fr) auto; grid-template-areas:"d w a" "d w b" "k k p"; row-gap:var(--s1); }
  .ev .d { grid-area:d; font-size:18px; } .ev .w { grid-area:w; } .ev .a { grid-area:a; } .ev button { grid-area:b; justify-self:end; }
  .ev .k { grid-area:k; } .ev .p { grid-area:p; }
}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="grid">
      <h1 class="mark">{{APP}}<b>.</b></h1>
      <div class="tag" data-i18n="app.tagline"></div>
      <div class="langs" id="langs"></div>
    </div>
    <div class="pill"><i></i><span id="status-line"></span></div>
  </div>
</header>
<main class="wrap">
  <div class="grid">
    <p class="empty-line" id="empty-line" data-i18n="empty.line" hidden></p>
    <div class="drop" id="drop">
      <div><strong data-i18n="drop.title"></strong><span class="hint" data-i18n="drop.hint"></span></div>
      <div class="acts">
        <label class="btn" for="fdir" data-i18n="drop.folder"></label>
        <input type="file" id="fdir" webkitdirectory directory multiple>
        <label class="btn" for="ffiles" data-i18n="drop.files"></label>
        <input type="file" id="ffiles" multiple accept="application/pdf,.pdf">
      </div>
    </div>
    <div class="progress" id="progress"></div>

    <div class="tabs">
      <button id="tab-agenda" class="on" data-i18n="tab.agenda"></button>
      <button id="tab-papers" data-i18n="tab.papers"></button>
      <button id="tab-report" data-i18n="tab.report"></button>
    </div>

    <section class="panel agenda on" id="panel-agenda">
      <p class="today" id="agenda-line"></p>
      <div class="cal" id="cal">
        <div class="cal-head">
          <h2 id="cal-title"></h2>
          <button type="button" id="cal-prev" aria-label="prev">‹</button>
          <button type="button" id="cal-next" aria-label="next">›</button>
          <button type="button" id="cal-today" class="today" data-i18n="agenda.today_btn"></button>
        </div>
        <div class="cal-days" id="cal-days"></div>
        <div class="cal-grid" id="cal-grid"></div>
      </div>
      <div class="wk-title"><span class="label" data-i18n="agenda.week_title"></span></div>
      <div class="wk" id="wk"></div>
      <div class="figs" id="agenda-figs"></div>
      <div class="events" id="agenda-events"></div>
      <div class="reminders">
        <span class="label" data-i18n="agenda.reminders"></span>
        <a class="btn primary" id="ics" href="/api/agenda.ics" data-i18n="agenda.export"></a>
        <span class="hint" data-i18n="agenda.export_hint"></span>
      </div>
    </section>

    <section class="panel" id="panel-papers">
      <div class="list" id="list"></div>
      <div class="facts" id="facts"></div>
    </section>

    <section class="panel report" id="panel-report">
      <div class="bar">
        <label><span class="label" data-i18n="month.label"></span><select id="month"></select></label>
        <button class="primary" id="gen" data-i18n="report.generate"></button>
        <a class="btn" id="md" href="#" data-i18n="report.markdown"></a>
      </div>
      <div id="report"></div>
    </section>
  </div>
</main>

<div class="ask">
  <div class="wrap">
    <div class="chips" id="chips"></div>
    <form class="in" id="askform">
      <input id="q" type="text" autocomplete="off">
      <button class="primary" type="submit" id="askbtn" data-i18n="ask.button"></button>
    </form>
    <div class="out" id="out"></div>
  </div>
</div>

<script>
(function () {
  var S = {};                       // chaînes de la langue courante
  var state = { lang: 'fr', papers: [], selected: null, tab: 'agenda', today: null, month: null, day: null, agenda: null };
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  var T = function (k) { return S[k] || k; };
  var LANGS = ['fr', 'de', 'it', 'en'];
  var AMOUNT_KEYS = { amount_chf: 1, opening_balance: 1, closing_balance: 1 };
  var FIELD_ORDER = {
    qr_bill: ['issuer','recipient','amount_chf','currency','iban','reference','reference_type','invoice_number','invoice_date','due_date','purpose'],
    bank_statement: ['bank','account_iban','holder','period_start','period_end','opening_balance','closing_balance','movements','currency'],
    official_letter: ['authority','recipient','letter_date','subject','letter_kind','amount_chf','deadline','reference_number','action_required']
  };
  // Une valeur machine (pay, tax_decision…) en mots de la langue ; inconnue → telle quelle.
  function words(kind, v) { var k = kind + '.' + String(v); var w = S[k]; return (w === undefined) ? String(v) : (w === '' ? '—' : w); }
  function chf(v) { if (v == null || isNaN(v)) return '—'; var s = Math.abs(v).toFixed(2); s = s.replace(/\B(?=(\d{3})+(?!\d))/g, "'"); return (v < 0 ? '-' : '') + s; }

  // --- langue ------------------------------------------------------------
  function setLang(l, first) {
    state.lang = l;
    try { localStorage.setItem('papertus.lang', l); } catch (e) {}
    document.documentElement.lang = l;
    fetch('/api/strings?lang=' + l).then(function (r) { return r.json(); }).then(function (s) {
      S = s;
      document.querySelectorAll('[data-i18n]').forEach(function (el) { el.textContent = T(el.getAttribute('data-i18n')); });
      $('q').placeholder = T('ask.placeholder');
      $('status-line').textContent = S._status || '';
      renderLangs(); renderChips(); renderList(); renderFacts(); renderProgress(lastStatus);
      if (!first && state.tab === 'report' && $('report').innerHTML) genReport();
      if (state.tab === 'agenda') loadAgenda();
    });
  }
  function renderLangs() {
    $('langs').innerHTML = '<div class="sw">' + LANGS.map(function (l) {
      return '<button class="' + (l === state.lang ? 'on' : '') + '" data-l="' + l + '" aria-label="' + l + '">' + l.toUpperCase() + '</button>'; }).join('') + '</div>';
    $('langs').querySelectorAll('button').forEach(function (b) { b.onclick = function () { setLang(b.getAttribute('data-l')); }; });
  }
  function renderChips() {
    var keys = ['ask.chip.1', 'ask.chip.2', 'ask.chip.3', 'ask.chip.4', 'ask.chip.5'];
    $('chips').innerHTML = '<span class="label">' + esc(T('ask.examples')) + '</span>' + keys.map(function (k) {
      return '<button type="button" data-q="' + esc(T(k)) + '">' + esc(T(k)) + '</button>'; }).join('');
    $('chips').querySelectorAll('button').forEach(function (b) { b.onclick = function () { $('q').value = b.getAttribute('data-q'); $('askform').requestSubmit ? $('askform').requestSubmit() : $('askbtn').click(); }; });
  }

  // --- liste et fiche ------------------------------------------------------
  function loadPapers() {
    return fetch('/api/papers').then(function (r) { return r.json(); }).then(function (ps) {
      var before = JSON.stringify(state.papers.filter(function (p) { return p.id === state.selected; })[0] || null);
      state.papers = ps;
      if (state.selected && !ps.some(function (p) { return p.id === state.selected; })) state.selected = null;
      var after = JSON.stringify(ps.filter(function (p) { return p.id === state.selected; })[0] || null);
      $('empty-line').hidden = ps.length > 0;
      renderList(); loadMonths();
      if (before !== after || !state.selected) renderFacts();  // la fiche ne bouge que si son papier a changé
      if (state.tab === 'agenda') loadAgenda();
    });
  }
  function renderList() {
    var el = $('list');
    if (!state.papers.length) { el.innerHTML = '<div class="empty">' + esc(T('papers.empty')) + '</div>'; return; }
    el.innerHTML = state.papers.map(function (p) {
      return '<button class="row' + (p.id === state.selected ? ' on' : '') + '" data-id="' + esc(p.id) + '">' +
        '<span class="k">' + esc(T('kind.' + p.kind)) + '</span>' +
        '<span class="f">' + esc(p.id) + ' · ' + esc(p.file) + '</span>' +
        '<span class="l">' + esc(p.language) + '</span></button>'; }).join('');
    el.querySelectorAll('.row').forEach(function (b) { b.onclick = function () { state.selected = b.getAttribute('data-id'); renderList(); renderFacts(); }; });
  }
  function glyph(st) { return st === 'verified' ? '✓' : (st === 'model' ? '•' : '—'); }
  function fieldRow(key, f) {
    var st = f ? f.status : 'blank', v = f ? f.value : null, reason = f ? f.reason : 'missing';
    var html = '<tr><th>' + esc(T('field.' + key)) + '</th><td><span class="g ' + st + '" title="' + esc(T('status.' + st)) + '">' + glyph(st) + '</span></td>';
    if (v == null) {
      html += '<td class="v blank">' + esc(T('status.blank')) + (reason ? '<span class="reason">' + esc(T('status.reason')) + ' : ' + esc(reason) + '</span>' : '') + '</td>';
    } else if (key === 'movements' && Array.isArray(v)) {
      html += '<td class="v"><table class="mov">' + v.map(function (m) {
        return '<tr><td class="num">' + esc(m.date || '') + '</td><td>' + esc(m.label || '') + '</td><td class="num">' + chf(m.amount) + '</td></tr>'; }).join('') + '</table>';
      if (reason) html += '<span class="reason">' + esc(reason) + '</span>';
      html += '</td>';
    } else {
      var isAmt = (typeof v === 'number') && AMOUNT_KEYS[key];
      var shown = (typeof v === 'number') ? chf(v) : (key === 'letter_kind' ? words('letter_kind', v) : (key === 'action_required' ? words('action', v) : String(v)));
      html += '<td class="v' + (isAmt ? ' amt' : '') + '">' + esc(shown) + (st === 'model' && reason ? '<span class="reason">' + esc(reason) + '</span>' : '') +
        (st === 'verified' && f.source ? '<span class="reason">' + esc(T('status.verified')) + ' · ' + esc(f.source) + '</span>' : '') + '</td>';
    }
    return html + '</tr>';
  }
  function renderFacts() {
    var el = $('facts');
    var p = state.papers.filter(function (x) { return x.id === state.selected; })[0];
    if (!p) { el.innerHTML = '<div class="empty">' + esc(state.papers.length ? T('paper.select') : T('papers.empty')) + '</div>'; return; }
    var keys = FIELD_ORDER[p.kind] || Object.keys(p.facts || {});
    var rows = keys.map(function (k) { return fieldRow(k, (p.facts || {})[k]); }).join('');
    el.innerHTML = '<span class="label">' + esc(T('kind.' + p.kind)) + '</span><h2>' + esc(p.id) + '</h2>' +
      '<div class="meta"><span>' + esc(p.file) + '</span><span>' + esc(p.language) + ' · ' + p.pages + ' ' + esc(T('paper.pages')) + '</span>' +
      '<a class="btn" href="/api/paper/' + esc(p.id) + '/file" target="_blank">' + esc(T('paper.open')) + '</a></div>' +
      (p.read_with && p.read_with.size ? '<p class="readwith">' + esc(T('paper.read_with').replace('{model}',
        T(p.read_with.local ? 'status.local' : 'status.cloud').replace('{size}', p.read_with.size))) + '</p>' : '') +
      '<table><colgroup><col class="c1"><col class="c2"><col></colgroup>' + rows + '</table>' +
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
    ['agenda', 'papers', 'report'].forEach(function (x) {
      $('tab-' + x).classList.toggle('on', x === t); $('panel-' + x).classList.toggle('on', x === t); });
    if (t === 'agenda') loadAgenda();
  }
  $('tab-agenda').onclick = function () { showTab('agenda'); };
  $('tab-papers').onclick = function () { showTab('papers'); };
  function openPaper(id) { state.selected = id; showTab('papers'); renderList(); renderFacts(); window.scrollTo(0, 0); }

  // --- agenda : tout vient de /api/agenda, calculé par le code ------------------
  function fmtDay(iso) { var p = iso.split('-'); return p[2] + '.' + p[1]; }
  function loadAgenda() {
    var url = '/api/agenda?lang=' + state.lang + (state.today ? '&today=' + state.today : '') + (state.month ? '&month=' + state.month : '');
    $('ics').href = '/api/agenda.ics?lang=' + state.lang + (state.today ? '&today=' + state.today : '');
    fetch(url).then(function (r) { return r.json(); }).then(renderAgenda).catch(function () { $('agenda-line').textContent = T('error'); });
  }
  var MONTHS = { fr: ['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre'],
    de: ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember'],
    it: ['gennaio','febbraio','marzo','aprile','maggio','giugno','luglio','agosto','settembre','ottobre','novembre','dicembre'],
    en: ['January','February','March','April','May','June','July','August','September','October','November','December'] };
  function monthLabel(ym) { var p = ym.split('-'); var n = MONTHS[state.lang][parseInt(p[1], 10) - 1]; return n.charAt(0).toUpperCase() + n.slice(1) + ' ' + p[0]; }
  function renderCalendar(a) {
    var m = a.month, days = T('agenda.weekdays').split(' ');
    $('cal-title').textContent = monthLabel(m.month);
    $('cal-days').innerHTML = days.map(function (d) { return '<span>' + esc(d) + '</span>'; }).join('');
    var html = '';
    m.weeks.forEach(function (week) { week.forEach(function (c) {
      var cls = 'cell' + (c.in_month ? '' : ' out') + (c.weekend ? ' we' : '') + (c.today ? ' today' : '') + (state.day === c.date ? ' sel' : '');
      var shown = c.events.slice(0, 3), more = c.events.length - shown.length;
      var chips = shown.map(function (e) {
        return '<span class="chip ' + e.kind + (e.overdue ? ' late' : '') + (e.done ? ' done' : '') + '" title="' + esc((e.who || '') + (e.amount != null ? ' · CHF ' + chf(e.amount) : '')) + '">' + esc(e.who || e.paper_id) + '</span>'; }).join('') +
        (more > 0 ? '<span class="chip more">+' + more + '</span>' : '');
      var kinds = []; c.events.forEach(function (e) { var k = e.kind + (e.overdue ? ' late' : ''); if (kinds.indexOf(k) < 0) kinds.push(k); });
      var dots = '<span class="dots">' + kinds.slice(0, 3).map(function (k) { return '<i class="dot ' + k + '"></i>'; }).join('') + '</span>';
      html += '<button type="button" class="' + cls + '" data-day="' + c.date + '"><span class="dn">' + c.day + '</span>' + chips + dots +
        (c.open_bills_total ? '<span class="sum">' + chf(c.open_bills_total) + '</span>' : '') + '</button>';
    }); });
    $('cal-grid').innerHTML = html;
    $('cal-grid').querySelectorAll('.cell').forEach(function (b) { b.onclick = function () {
      var d = b.getAttribute('data-day'); state.day = (state.day === d) ? null : d; renderCalendar(a); renderEvents(a); }; });
    $('cal-prev').onclick = function () { state.month = m.prev; state.day = null; loadAgenda(); };
    $('cal-next').onclick = function () { state.month = m.next; state.day = null; loadAgenda(); };
    $('cal-today').onclick = function () { state.month = null; state.day = null; loadAgenda(); };
  }
  function renderWeek(a) {
    var days = T('agenda.weekdays').split(' ');
    $('wk').innerHTML = a.week.map(function (d) {
      return '<div class="wd' + (d.today ? ' today' : '') + '"><div class="m">' + esc(days[d.weekday]) + '</div><div class="n">' + d.day + '</div>' +
        '<div class="c">' + (d.count ? '<b>' + d.count + '</b>' + (d.sum ? ' · ' + chf(d.sum) : '') : '—') + '</div></div>'; }).join('');
  }
  function renderAgenda(a) {
    state.agenda = a;
    renderCalendar(a); renderWeek(a);
    var f = a.figures;
    $('agenda-line').innerHTML = '<b>' + esc(T('agenda.today.label')) + ' · ' + esc(a.today) + '</b>' + esc(a.line);
    var plural = function (k, n) { return T(n === 1 ? k + '_1' : k + '_n').replace('{n}', n); };
    $('agenda-figs').innerHTML =
      '<div class="fig"><div class="n">' + chf(f.due_7_total) + '</div><div class="u">' + esc(T('agenda.fig.due7')) + ' · ' + esc(plural('agenda.fig.count', f.due_7_count)) + '</div></div>' +
      '<div class="fig' + (f.overdue_count ? ' red' : '') + '"><div class="n">' + chf(f.overdue_total) + '</div><div class="u">' + esc(T('agenda.fig.overdue')) + ' · ' + esc(plural('agenda.fig.count', f.overdue_count)) + '</div></div>' +
      '<div class="fig"><div class="n">' + f.deadlines_month + '</div><div class="u">' + esc(T('agenda.fig.deadlines')) + '</div></div>' +
      '<div class="fig"><div class="n">' + chf(f.balance_total) + '</div><div class="u">' + esc(T('agenda.fig.balance')) + (f.balance_blank ? ' · ' + esc(plural('agenda.fig.blank', f.balance_blank)) : '') + '</div></div>';
    renderEvents(a);
  }
  function renderEvents(a) {
    var html = '', any = false;
    var groups = ['overdue', 'week', 'month', 'later', 'done'];
    if (state.day) {
      var all = []; groups.forEach(function (g) { (a.groups[g] || []).forEach(function (e) { if (e.date === state.day) all.push(e); }); });
      html += '<div class="dayfilter"><span>' + esc(T('agenda.day_filter')) + ' ' + esc(fmtDay(state.day)) + '.' + esc(state.day.slice(0, 4)) + ' · ' + all.length + '</span><a href="#" id="day-all">' + esc(T('agenda.all')) + '</a></div>';
      a = { groups: { day: all } }; groups = ['day']; any = all.length > 0;
    }
    groups.forEach(function (g) {
      var evs = a.groups[g] || []; if (!evs.length) return; any = true;
      if (g !== 'day') html += '<h3 class="' + (g === 'overdue' ? 'red' : '') + '">' + esc(T('agenda.group.' + g)) + ' · ' + evs.length + '</h3>';
      evs.forEach(function (e) {
        var amt = e.amount == null
          ? '<div class="a blank">' + esc(e.kind === 'statement' ? T('agenda.unverified') : '—') + '</div>'
          : '<div class="a">' + chf(e.amount) + (e.kind === 'statement' ? '<span class="st">' + esc(T('field.closing_balance')) + '</span>' : '') + '</div>';
        var why = (e.kind === 'statement' && e.amount == null && e.amount_reason) ? '<div class="why">' + esc(e.amount_reason) + '</div>' : '';
        var btn = e.kind === 'bill' ? 'agenda.paid' : 'agenda.done';
        html += '<div class="ev' + (e.done ? ' done' : '') + '" data-id="' + esc(e.id) + '">' +
          '<div class="d">' + esc(fmtDay(e.date)) + '<small>' + esc(e.date.slice(0, 4)) + '</small></div>' +
          '<div class="k">' + esc(T('agenda.kind.' + e.kind)) + (e.action && e.action !== 'none' ? '<small>' + esc(words('action', e.action)) + '</small>' : '') + '</div>' +
          '<div class="w"><div class="who">' + esc(e.who || '—') + '</div>' + (e.what ? '<div class="what" title="' + esc(e.what) + '">' + esc(e.what) + '</div>' : '') + why + '</div>' + amt +
          '<button type="button" class="' + (e.done ? 'on' : '') + '" data-done="' + (e.done ? '0' : '1') + '" title="' + esc(e.done ? T('agenda.undo') : '') + '">' + esc(e.done ? '✓ ' + T(btn) : T(btn)) + '</button>' +
          '<div class="p"><a class="pid" href="#" data-open="' + esc(e.paper_id) + '">' + esc(e.paper_id) + '</a></div></div>';
      });
    });
    $('agenda-events').innerHTML = any ? html : '<p class="muted">' + esc(T('agenda.empty')) + '</p>';
    $('agenda-events').querySelectorAll('button[data-done]').forEach(function (b) {
      b.onclick = function () {
        var id = b.closest('.ev').getAttribute('data-id'), done = b.getAttribute('data-done') === '1';
        fetch('/api/agenda/done', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: id, done: done }) })
          .then(function () { loadAgenda(); });
      };
    });
    $('agenda-events').querySelectorAll('a[data-open]').forEach(function (l) { l.onclick = function (e) { e.preventDefault(); openPaper(l.getAttribute('data-open')); }; });
    var all = $('day-all'); if (all) all.onclick = function (e) { e.preventDefault(); state.day = null; renderCalendar(state.agenda); renderEvents(state.agenda); };
  }
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
          (a.by === 'code' ? ' · ' + esc(T('ask.by_code')) : (a.by === 'model' ? ' · ' + esc(S._badge || T('ask.by_model')) : '')) +
          (a.note ? ' · ' + esc(a.note) : '') + '</div>';
        $('askbtn').disabled = false;
      }).catch(function () { out.innerHTML = '<span class="muted">' + esc(T('error')) + '</span>'; $('askbtn').disabled = false; });
  };

  // --- la barre de question ne couvre jamais le contenu -----------------------
  function padForAsk() { document.body.style.paddingBottom = (document.querySelector('.ask').offsetHeight + 24) + 'px'; }
  window.addEventListener('resize', padForAsk); padForAsk();
  new MutationObserver(padForAsk).observe($('out'), { childList: true, subtree: true });

  // --- départ ---------------------------------------------------------------
  // L'adresse peut préciser ?lang=de&paper=p050&tab=report&q=… (lien partageable, captures d'écran).
  var params = new URLSearchParams(location.search);
  var saved = 'fr'; try { saved = localStorage.getItem('papertus.lang') || 'fr'; } catch (e) {}
  if (params.get('lang')) saved = params.get('lang');
  if (LANGS.indexOf(saved) < 0) saved = 'fr';
  if (params.get('paper')) state.selected = params.get('paper');
  state.today = /^\d{4}-\d{2}-\d{2}$/.test(params.get('today') || '') ? params.get('today') : null;
  state.month = /^\d{4}-\d{2}$/.test(params.get('month') || '') ? params.get('month') : null;
  setLang(saved, true);
  loadPapers().then(function () {
    var tab = params.get('tab');
    if (tab === 'report') { showTab('report'); genReport(); }
    else if (tab === 'papers' || params.get('paper') || params.get('q')) showTab('papers');
    else if (tab === 'agenda' || state.papers.length) showTab('agenda');
    else showTab('papers');
    // ?q=… pose la question à l'ouverture (lien partageable, captures d'écran).
    if (params.get('q')) { $('q').value = params.get('q'); $('askform').onsubmit(new Event('submit')); }
  });
  poll();
})();
</script>
</body>
</html>
"""
