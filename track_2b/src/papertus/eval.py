"""L'évaluation : Apertus seul (Baseline) contre Apertus + code (Ours), sur data/.

    python -m papertus.eval --data data --out docs [--limit N] [--methods ours|baseline|both]
                             [--questions N] [--workers 4] [--no-cache]

Pour chaque papier et chaque champ compté (nombres, dates, identifiants) on compare
à la vérité : correct / inventé (une valeur qui diffère de la vérité) / blanc. Le
titre, c'est le taux d'inventé. Puis les questions de data/questions.json : réponse
exacte ou non. Jetons et temps par papier. Les résultats vont dans docs/results.md
(tableau Setup / Metric / Result) et docs/results.json. Un cache dans runtime/ évite
de payer deux fois le même papier.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any

from . import ask, baseline, i18n, llm, read, verify
from .schema import DATE_KEYS, NUMERIC_KEYS, Paper, Truth, load_truth, scored_keys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "runtime", "cache")  # changé par --tag (runtime/cache-<tag>)
NO_NEW_CALLS = False  # --no-new-calls : rejouer les contrôles sur le cache, sans appel au modèle
_blocked = threading.local()  # drapeau par fil : « ask aurait eu besoin du modèle »


def _blocked_chat(*args: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
    """Remplace llm.chat sous --no-new-calls : aucun appel ne part, on note que le modèle manquait."""
    _blocked.hit = True
    raise llm.ModelError("model call blocked by --no-new-calls")
TOP_PAPERS = 6
NOT_FOUND = {
    "fr": "Je ne trouve pas ce chiffre dans vos papiers.",
    "de": "Ich finde diese Zahl in Ihren Papieren nicht.",
    "it": "Non trovo questa cifra nei suoi documenti.",
    "en": "I cannot find this figure in your papers.",
}
NO_PAPER = {
    "fr": "Aucun papier ne répond à cette question.",
    "de": "Kein Papier beantwortet diese Frage.",
    "it": "Nessun documento risponde a questa domanda.",
    "en": "No paper answers this question.",
}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Cache (runtime/, ignoré par git)
# ---------------------------------------------------------------------------

def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()[:16]


def _cache_path(method: str, key: str) -> str:
    d = os.path.join(RUNTIME, method)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, key + ".json")


def _cache_get(method: str, key: str, stamp: str) -> dict[str, Any] | None:
    p = _cache_path(method, key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    return got if got.get("stamp") == stamp else None


def _cache_put(method: str, key: str, stamp: str, payload: dict[str, Any]) -> None:
    payload = dict(payload, stamp=stamp)
    with open(_cache_path(method, key), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Lire un papier avec une méthode, en mesurant jetons et temps
# ---------------------------------------------------------------------------

def run_paper(method: str, truth: Truth, pdf_path: str, use_cache: bool) -> dict[str, Any]:
    # L'empreinte porte sur le texte et le QR extraits (gratuits), pas sur les octets du PDF :
    # un papier regénéré à l'identique ne coûte rien de plus.
    text, qr, pages = read.read_pdf(pdf_path)
    stamp = hashlib.sha256((text + "\n" + (qr or "")).encode()).hexdigest()[:16] + ":" + (os.getenv("LLM_NAME") or llm.DEFAULT_MODEL)
    if use_cache and method == "ours":
        # La réponse brute du modèle suffit : la vérification (code pur) est rejouée à chaque fois,
        # ainsi une amélioration des contrôles ne coûte aucun appel.
        hit = _cache_get(method + "_raw", truth.id, stamp)
        if hit:
            paper = read.build_paper(paper_id=truth.id, file=truth.file, **hit["got"])
            return {"paper": paper.to_dict(), "tokens": hit["tokens"], "seconds": hit["seconds"]}
    if use_cache and method != "ours":
        hit = _cache_get(method, truth.id, stamp)
        if hit:
            return hit
    if NO_NEW_CALLS:
        raise RuntimeError(f"{truth.id}: not in cache and --no-new-calls is set")
    t0 = time.time()
    with llm.usage_scope() as usage:
        if method == "ours":
            got = read.read_raw_from(text, qr, pages)
            paper = read.build_paper(paper_id=truth.id, file=truth.file, **got)
            _cache_put(method + "_raw", truth.id, stamp, {"got": got, "tokens": dict(usage), "seconds": round(time.time() - t0, 2)})
        else:
            paper = baseline.baseline_paper(pdf_path, paper_id=truth.id, rel_file=truth.file, text=text, pages=pages)
    out = {"paper": paper.to_dict(), "tokens": dict(usage), "seconds": round(time.time() - t0, 2)}
    if method != "ours":
        _cache_put(method, truth.id, stamp, out)
    return out


# ---------------------------------------------------------------------------
# Comparer un champ à la vérité
# ---------------------------------------------------------------------------

def _norm_id(v: Any) -> str:
    return re.sub(r"\s+", "", str(v)).lower()


def same_value(key: str, got: Any, truth: Any) -> bool:
    if got is None or truth is None:
        return got is None and truth is None
    if key in NUMERIC_KEYS:
        g, t = verify.parse_amount(got), verify.parse_amount(truth)
        return g is not None and t is not None and abs(g - t) < 0.01
    if key in DATE_KEYS:
        g, t = verify.parse_iso_date(got), verify.parse_iso_date(truth)
        return g is not None and g == t
    return _norm_id(got) == _norm_id(truth)


def judge_field(key: str, got: Any, truth: Truth) -> str:
    """'correct' | 'invented' | 'blank' pour un champ."""
    expected_blank = truth.expected_blank(key)
    if got is None:
        return "correct" if expected_blank else "blank"
    if expected_blank:
        return "invented"
    return "correct" if same_value(key, got, truth.facts.get(key)) else "invented"


def score_paper(paper: dict[str, Any], truth: Truth) -> dict[str, Any]:
    facts = paper.get("facts", {})
    kind_ok = paper.get("kind") == truth.kind
    per_field: dict[str, str] = {}
    for key in scored_keys(truth.kind):
        got = (facts.get(key) or {}).get("value") if kind_ok else None
        per_field[key] = judge_field(key, got, truth)
    # Les mouvements : comptés à part (liste), corrects si dates+montants coïncident.
    mov = None
    if truth.kind == "bank_statement":
        tm = truth.facts.get("movements") or []
        gm = (facts.get("movements") or {}).get("value") if kind_ok else None
        if not gm:
            mov = "blank"
        else:
            pairs_t = sorted((str(m.get("date")), round(float(m.get("amount", 0)), 2)) for m in tm)
            pairs_g = sorted((str(m.get("date")), round(float(verify.parse_amount(m.get("amount")) or 0), 2)) for m in gm if isinstance(m, dict))
            mov = "correct" if pairs_t == pairs_g else "invented"
    return {"id": truth.id, "kind": truth.kind, "kind_ok": kind_ok, "fields": per_field, "movements": mov, "traps": truth.traps}


# ---------------------------------------------------------------------------
# Les questions
# ---------------------------------------------------------------------------

def load_questions(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data.get("questions", data) if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    for i, q in enumerate(items):
        if not isinstance(q, dict):
            continue
        text = q.get("question") or q.get("q") or q.get("text") or ""
        ids = q.get("papers") or q.get("paper_ids") or q.get("ids") or q.get("sources") or []
        if isinstance(ids, str):
            ids = [ids]
        out.append({
            "id": q.get("id") or f"q{i + 1:03d}",
            "question": text,
            "truth": q.get("truth", q.get("answer")),
            "type": q.get("type") or "amount",
            "language": q.get("language") or q.get("lang") or guess_question_language(text),
            "papers": list(ids),
        })
    return out


def guess_question_language(q: str) -> str:
    low = q.lower()
    if re.search(r"\b(welche|wie|was|ist|am|der|die|das|ich|bis|fällig|saldo am)\b", low):
        return "de"
    if re.search(r"\b(quante|quanto|qual|quale|ho|il|della|del|in settembre|scadenza)\b", low):
        return "it"
    if re.search(r"\b(combien|quel|quelle|quels|dois|solde|délai|factures|reçues)\b", low):
        return "fr"
    return "fr"


_MONTHS_Q = {
    "janvier": 1, "januar": 1, "gennaio": 1, "février": 2, "februar": 2, "febbraio": 2, "mars": 3, "märz": 3, "marzo": 3,
    "avril": 4, "april": 4, "aprile": 4, "mai": 5, "maggio": 5, "juin": 6, "juni": 6, "giugno": 6, "juillet": 7, "juli": 7,
    "luglio": 7, "août": 8, "august": 8, "agosto": 8, "septembre": 9, "september": 9, "settembre": 9, "octobre": 10,
    "oktober": 10, "ottobre": 10, "novembre": 11, "november": 11, "dicembre": 12, "décembre": 12, "dezember": 12,
}
_KIND_WORDS = {
    "qr_bill": re.compile(r"facture|rechnung|fattur|dois|schulde|devo|fällig|échéance|scad|payer|bezahlen|pagare|total", re.I),
    "bank_statement": re.compile(r"solde|saldo|kontostand|compte|konto|conto|relev|auszug|estratto", re.I),
    "official_letter": re.compile(r"délai|frist|termine|lettre|brief|lettera|office|amt|ufficio|commune|gemeinde|comune|décision|verfügung|decisione", re.I),
}
_STOP = set("combien dois je encore quel quelle quels est le la les du de des au aux en et à a pour welche sind wie viel ist der die das am ich bis quante quanto qual quale ho il della del in von vom per con che mon ma mes compte total factures reçues rechnungen fatture ricevute montant betrag importo avec".split())


def _paper_values(p: Paper) -> list[str]:
    vals: list[str] = []
    for f in p.facts.fields.values():
        if f.value is None or isinstance(f.value, (list, dict, bool)):
            continue
        vals.append(str(f.value))
    return vals


def _paper_dates(p: Paper) -> list[date]:
    out = []
    for k, f in p.facts.fields.items():
        if k in DATE_KEYS and f.value:
            d = verify.parse_iso_date(f.value)
            if d:
                out.append(d)
    return out


MAX_SELECTED = 20
_COUNT_TOTAL_WORDS = re.compile(r"combien de|wie viele|quante|quanti|total|summe|totale|somme", re.I)


def select_papers(question: str, papers: list[Paper]) -> list[Paper]:
    """Les papiers que la question vise, choisis par du code (pas de modèle).

    Trois cas, du plus sûr au plus vague : une date exacte dans la question → tous les
    papiers qui portent cette date ; un mois (compte / total) → toutes les factures émises
    ce mois-là ; sinon les papiers dont les faits parlent des mêmes mots (top 6).
    """
    low = question.lower()
    words = {w for w in re.findall(r"[a-zà-ÿ0-9][a-zà-ÿ0-9'’\-]{2,}", low) if w not in _STOP}
    q_dates = verify.dates_in_text(question)
    q_months = {m for name, m in _MONTHS_Q.items() if re.search(r"\b" + re.escape(name) + r"\b", low)}
    kinds = {k for k, rx in _KIND_WORDS.items() if rx.search(question)}

    def word_score(p: Paper) -> float:
        blob = " ".join(_paper_values(p)).lower()
        score = sum((3 if len(w) > 4 else 1) for w in words if w in blob)
        # Un identifiant exact (2026-91289-LU, 2026-118) vaut plus que tous les mots.
        score += sum(10 for w in words if re.search(r"\d", w) and "-" in w and w in blob)
        if kinds and p.kind in kinds:
            score += 1.5
        return score

    # Un identifiant cité par sa fin (« compte se terminant par 5840 ») : le code fait la comparaison.
    q_suffixes = {t for t in re.findall(r"\b\d{4,}\b", re.sub(r"\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2}", " ", low)) if not t.startswith("20")}

    def id_match(p: Paper) -> bool:
        ids = [str(p.facts.get(k) or "") for k in ("account_iban", "iban", "reference", "invoice_number", "reference_number")]
        return any(i.endswith(t) for t in q_suffixes for i in ids if i)

    # 0. Un identifiant précis (numéro de facture, référence, fin de compte) désigne le papier.
    strong = [p for p in papers if id_match(p)] if q_suffixes else []
    dashed = {w for w in words if re.search(r"\d", w) and "-" in w}
    if dashed:
        strong += [p for p in papers if p not in strong and any(
            w == str(p.facts.get(k) or "").lower() for w in dashed for k in ("invoice_number", "reference_number", "reference"))]
    if strong:
        strong.sort(key=lambda p: (-word_score(p), p.id))
        return strong[:MAX_SELECTED]

    # 1. Une date exacte : tous les papiers qui la portent (solde au …, factures dues le …).
    if q_dates:
        iso = {d.isoformat() for d in q_dates}
        if re.search(r"fällig|dues?\b|due\b|scad|échéance|zahlbar", low):
            exact = [p for p in papers if p.facts.get("due_date") in iso]
        elif re.search(r"solde|saldo|kontostand", low):
            exact = [p for p in papers if p.facts.get("period_end") in iso]
        else:
            exact = []
        if not exact:
            exact = [p for p in papers if q_dates & set(_paper_dates(p))]
        if exact and kinds and any(p.kind in kinds for p in exact):
            exact = [p for p in exact if p.kind in kinds]
        if q_suffixes and any(id_match(p) for p in exact):
            exact = [p for p in exact if id_match(p)]
        if exact:
            exact.sort(key=lambda p: (-word_score(p), p.id))
            return exact[:MAX_SELECTED]

    # 2. Un mois et une question de compte ou de total : toutes les factures émises ce mois-là.
    if q_months and _COUNT_TOTAL_WORDS.search(low):
        month_bills = []
        for p in papers:
            d = verify.parse_iso_date(p.facts.get("invoice_date")) if p.kind == "qr_bill" else None
            if d and d.month in q_months:
                month_bills.append(p)
        month_bills.sort(key=lambda p: p.id)
        return month_bills[:MAX_SELECTED]

    # 3. Les mots de la question contre les faits (noms, numéros).
    scored = [(word_score(p), p) for p in papers]
    scored = [(sc, p) for sc, p in scored if sc > 0]
    scored.sort(key=lambda t: (-t[0], t[1].id))
    return [p for _, p in scored[:TOP_PAPERS]]


def _facts_digest(p: Paper) -> str:
    parts = []
    for k, f in p.facts.fields.items():
        if k == "balanced":
            continue
        if f.value is None:
            parts.append(f"{k}: BLANK (not verifiable: {(f.reason or 'unknown').split(':')[0]})")
            continue
        if isinstance(f.value, list):
            parts.append(f"{k}: {len(f.value)} lines")
            continue
        tick = "verified" if f.status == "verified" else "model"
        parts.append(f"{k}: {f.value} ({tick})")
    return f"[{p.id}] kind={p.kind} lang={p.language}; " + "; ".join(parts)


def _aggregates(selected: list[Paper], question: str = "") -> dict[str, Any]:
    """Ce que le code peut calculer sur les papiers retenus : compte, total, par mois, solde à une date."""
    bills = [p for p in selected if p.kind == "qr_bill"]
    q_dates = verify.dates_in_text(question)
    amounts = [verify.parse_amount(p.facts.get("amount_chf")) for p in bills]
    amounts = [a for a in amounts if a is not None]
    agg: dict[str, Any] = {"selected_ids": [p.id for p in selected], "paper_count": len(selected),
                           "bill_count": len(bills), "bills_total_chf": round(sum(amounts), 2)}
    by_month: dict[str, dict[str, Any]] = {}
    for p in bills:
        d = verify.parse_iso_date(p.facts.get("invoice_date"))
        a = verify.parse_amount(p.facts.get("amount_chf"))
        if d:
            m = by_month.setdefault(f"{d.year}-{d.month:02d}", {"count": 0, "total_chf": 0.0, "ids": []})
            m["count"] += 1
            m["ids"].append(p.id)
            if a is not None:
                m["total_chf"] = round(m["total_chf"] + a, 2)
    agg["bills_by_invoice_month"] = by_month
    suffixes = {t for t in re.findall(r"\b\d{4,}\b", re.sub(r"\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2}", " ", question)) if not t.startswith("20")}
    if suffixes:
        agg["account_ending_with"] = {t: [p.id for p in selected if str(p.facts.get("account_iban") or p.facts.get("iban") or "").endswith(t)] for t in suffixes}
    letters = [p for p in selected if p.kind == "official_letter"]
    if letters:
        agg["letter_deadlines"] = {p.id: p.facts.get("deadline") for p in letters}
    if q_dates:
        iso = {d.isoformat() for d in q_dates}
        agg["bills_due_on_date"] = [p.id for p in bills if p.facts.get("due_date") in iso]
        agg["closing_balance_on_date"] = {
            p.id: p.facts.get("closing_balance") for p in selected
            if p.kind == "bank_statement" and p.facts.get("period_end") in iso and p.facts.get("closing_balance") is not None}
    return agg


def answer_question(question: str, papers: list[Paper]) -> str:
    """Réponse ancrée : papiers choisis par le code, Apertus cite [pNNN], chiffres vérifiés.

    (ask.py remplacera ce chemin par la recherche FTS5 ; la logique de garde reste la même.)
    """
    lang = guess_question_language(question)
    selected = select_papers(question, papers)
    if not selected:
        return NO_PAPER[lang]
    agg = _aggregates(selected, question)
    context = "\n".join(_facts_digest(p) for p in selected)
    computed = json.dumps(agg, ensure_ascii=False)
    messages = [
        {"role": "system", "content": (
            "You answer a question about the user's Swiss papers using ONLY the facts below. "
            "Answer in the language of the question, in one short sentence. Cite the paper ids in square "
            "brackets like [p012]. Use the numbers exactly as given; for counts and totals use the "
            "COMPUTED block, never your own arithmetic. A field marked BLANK is unknown: if the asked value "
            "is BLANK, say you cannot find it, never substitute another field. If the facts do not answer the question, say so.")},
        {"role": "user", "content": f"FACTS:\n{context}\n\nCOMPUTED (by code, over the facts above): {computed}\n\nQUESTION: {question}"},
    ]
    try:
        text, _ = llm.chat(messages, json=False, max_tokens=150)
    except (llm.ModelError, llm.ModelAnswerError) as e:
        log(f"  question: model error, answering not-found: {str(e)[:100]}")
        return NOT_FOUND[lang]
    answer = text.strip()
    cited = set(re.findall(r"\[(p\d{3,4})\]", answer))
    by_id = {p.id: p for p in selected}
    grounding: list[Any] = [agg]
    grounding += [by_id[c].facts for c in cited if c in by_id]
    if not cited:
        grounding += [p.facts for p in selected]
    ok, _missing = verify.numbers_grounded(answer, grounding)
    if not ok:
        return NOT_FOUND[lang]
    return answer


def _numbers_in_answer(answer: str) -> list[float]:
    """Les nombres d'une réponse (hors citations et dates), 1'234.5 comme 1234.50."""
    t = re.sub(r"\[p\d{3,4}\]", " ", answer)
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{4}\b", " ", t)
    t = verify._normalise_prose(t)
    out: list[float] = []
    for m in re.finditer(r"\d+(?:[.,]\d+)?", t):
        try:
            out.append(round(float(m.group(0).replace(",", ".")), 2))
        except ValueError:
            continue
    return out


_FALLBACK_LINES = set(NOT_FOUND.values()) | set(NO_PAPER.values()) | {
    i18n.t(key, lg) for key in ("ask.not_found", "ask.no_paper") for lg in ("fr", "de", "it", "en")}


def answer_is_blank(answer: str) -> bool:
    """Une réponse « je ne trouve pas » (nos lignes de repli) ou sans aucun chiffre."""
    a = (answer or "").strip()
    return not a or a in _FALLBACK_LINES or not re.search(r"\d", a)


def question_match(q: dict[str, Any], answer: str, value: Any = None) -> bool:
    """Réponse exacte : le chiffre / la date / la liste de la vérité est dans la réponse.

    Strict : une réponse blanche ne compte juste que si la vérité attend un blanc ; seule
    la réponse est lue (jamais une raison ou un commentaire). Quand le code a calculé une
    valeur nue (value), c'est elle qui est jugée, exactement."""
    truth = q.get("truth")
    qtype = q.get("type")
    if truth is None:
        return answer_is_blank(answer) and value is None
    if value is not None:
        if isinstance(value, list) or qtype == "list":
            want = {str(x) for x in truth} if isinstance(truth, list) else {str(truth)}
            return isinstance(value, list) and {str(x) for x in value} == want
        if qtype == "date":
            return verify.parse_iso_date(value) is not None and verify.parse_iso_date(value) == verify.parse_iso_date(truth)
        t, v = verify.parse_amount(truth), verify.parse_amount(value)
        return t is not None and v is not None and abs(t - v) < 0.01
    if answer_is_blank(answer):
        return False
    if qtype in ("amount", "count") or isinstance(truth, (int, float)):
        t = verify.parse_amount(truth)
        return t is not None and any(abs(n - t) < 0.01 for n in _numbers_in_answer(answer))
    if qtype == "date":
        td = verify.parse_iso_date(truth)
        return td is not None and td in verify.dates_in_text(answer)
    if qtype == "list" or isinstance(truth, list):
        want = {str(x) for x in truth} if isinstance(truth, list) else {str(truth)}
        got = set(re.findall(r"\bp\d{3,4}\b", answer))
        return got == want
    return _norm_id(truth) in _norm_id(answer)


def _facts_stamp(selected: list[Paper]) -> str:
    return hashlib.sha256(json.dumps([(p.id, p.facts.plain()) for p in selected], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _previous_answer(method_dir: str, q: dict[str, Any]) -> dict[str, Any] | None:
    """La dernière réponse en cache pour cette question (même id et même texte), quelle que soit la clé."""
    d = os.path.join(RUNTIME, method_dir)
    if not os.path.isdir(d):
        return None
    best: dict[str, Any] | None = None
    for name in os.listdir(d):
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            continue
        if c.get("id") == q["id"] and c.get("question") == q["question"]:
            if best is None or os.path.getmtime(os.path.join(d, name)) > best["_mtime"]:
                best = dict(c, _mtime=os.path.getmtime(os.path.join(d, name)))
    if best:
        best.pop("_mtime", None)
    return best


def run_question(method: str, q: dict[str, Any], papers: list[Paper], texts: dict[str, str], use_cache: bool) -> dict[str, Any]:
    """method : 'ours' (faits + calculs + citations + contrôle), 'baseline' (tous les textes bruts,
    contexte borné), 'baseline_retrieval' (les MÊMES papiers que notre sélection, en texte brut,
    réponse libre, aucun contrôle)."""
    selected = select_papers(q["question"], papers) if method != "baseline" else []
    if method == "ours_product" and NO_NEW_CALLS:
        # Le chemin produit est rejoué sur les faits en cache : les règles répondent ; si ask
        # avait besoin du modèle, la ligne est « none » (réponse vide), sans aucun appel.
        _blocked.hit = False
        t0 = time.time()
        a = ask.ask(q["question"], papers, q.get("language", "fr"))
        if getattr(_blocked, "hit", False):
            out = {"id": q["id"], "question": q["question"], "truth": q.get("truth"), "type": q.get("type"),
                   "answer": "", "match": question_match(q, ""), "papers_seen": [], "by": "none",
                   "note": "model needed, blocked by --no-new-calls", "cited_ids": [], "grounded": False, "value": None,
                   "tokens": {"total_tokens": 0}, "seconds": round(time.time() - t0, 2), "replayed": True}
        else:
            value = a.value if isinstance(a.value, (int, float, str, list)) else None
            out = {"id": q["id"], "question": q["question"], "truth": q.get("truth"), "type": q.get("type"),
                   "answer": a.text, "match": question_match(q, a.text, value), "papers_seen": [], "by": a.by,
                   "note": a.note, "cited_ids": list(a.cited_ids), "grounded": a.grounded, "value": value,
                   "tokens": {"total_tokens": 0}, "seconds": round(time.time() - t0, 2), "replayed": True}
        key = hashlib.sha256((q["question"] + "|" + _facts_stamp(papers)).encode()).hexdigest()[:20]
        _cache_put(method + "_q", key, os.getenv("LLM_NAME") or llm.DEFAULT_MODEL, out)
        return out
    if method == "ours_product":
        # Le produit (ask.py) regarde tous les papiers : la clé suit l'ensemble des faits.
        key_src = q["question"] + "|" + _facts_stamp(papers)
    elif method == "ours":
        key_src = q["question"] + "|" + _facts_stamp(selected)
    elif method == "baseline_retrieval":
        key_src = q["question"] + "|" + hashlib.sha256("\n".join(p.text for p in selected).encode()).hexdigest()[:16]
    else:
        key_src = q["question"] + "|" + ",".join(sorted(texts))
    key = hashlib.sha256(key_src.encode()).hexdigest()[:20]
    stamp = os.getenv("LLM_NAME") or llm.DEFAULT_MODEL
    if use_cache:
        hit = _cache_get(method + "_q", key, stamp)
        if hit:
            hit["match"] = question_match(q, hit["answer"], hit.get("value"))  # le juge peut changer, la réponse non
            return hit
    if NO_NEW_CALLS:
        # Les faits ont changé (contrôles rejoués) mais pas la question : on reprend la réponse
        # donnée lors du dernier vrai passage, re-jugée ; aucun appel au modèle.
        prev = _previous_answer(method + "_q", q)
        if prev:
            prev["match"] = question_match(q, prev["answer"], prev.get("value"))
            prev["replayed"] = True
            return prev
        return {"id": q["id"], "question": q["question"], "truth": q.get("truth"), "type": q.get("type"),
                "answer": "", "match": question_match(q, ""), "papers_seen": [], "tokens": {"total_tokens": 0}, "seconds": 0.0, "replayed": True}
    t0 = time.time()
    extra: dict[str, Any] = {}
    with llm.usage_scope() as usage:
        try:
            if method == "ours_product":
                a = ask.ask(q["question"], papers, q.get("language", "fr"))
                answer = a.text
                extra = {"by": a.by, "note": a.note, "cited_ids": list(a.cited_ids), "grounded": a.grounded,
                         "value": a.value if isinstance(a.value, (int, float, str, list)) else None}
            elif method == "ours":
                answer = answer_question(q["question"], papers)
            elif method == "baseline_retrieval":
                answer = baseline.baseline_questions({p.id: p.text for p in selected}, q["question"]) if selected else NO_PAPER[q.get("language", "fr")]
            else:
                answer = baseline.baseline_questions(texts, q["question"])
        except (llm.ModelError, llm.ModelAnswerError) as e:
            answer = f"(error: {str(e)[:60]})"
    out = {"id": q["id"], "question": q["question"], "truth": q.get("truth"), "type": q.get("type"),
           "answer": answer, "match": question_match(q, answer, extra.get("value")), "papers_seen": [p.id for p in selected],
           "tokens": dict(usage), "seconds": round(time.time() - t0, 2), **extra}
    _cache_put(method + "_q", key, stamp, out)
    return out


# ---------------------------------------------------------------------------
# Agréger et écrire
# ---------------------------------------------------------------------------

def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def summarise(method: str, runs: list[dict[str, Any]], scores: list[dict[str, Any]], qruns: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"correct": 0, "invented": 0, "blank": 0}
    per_kind: dict[str, dict[str, int]] = {}
    per_field: dict[str, dict[str, int]] = {}
    for s in scores:
        pk = per_kind.setdefault(s["kind"], {"correct": 0, "invented": 0, "blank": 0, "papers": 0, "kind_ok": 0})
        pk["papers"] += 1
        pk["kind_ok"] += int(s["kind_ok"])
        for key, verdict in s["fields"].items():
            counts[verdict] += 1
            pk[verdict] += 1
            pf = per_field.setdefault(f"{s['kind']}.{key}", {"correct": 0, "invented": 0, "blank": 0})
            pf[verdict] += 1
        if s.get("movements"):
            pf = per_field.setdefault("bank_statement.movements", {"correct": 0, "invented": 0, "blank": 0})
            pf[s["movements"]] += 1
    total = sum(counts.values())
    tokens = sum(r["tokens"]["total_tokens"] for r in runs)
    seconds = sum(r["seconds"] for r in runs)
    n = len(runs)
    qmatch = sum(int(q["match"]) for q in qruns)
    qtokens = sum(q["tokens"]["total_tokens"] for q in qruns)
    by = {"code": 0, "model": 0, "none": 0}
    for q in qruns:
        by[q.get("by") or "model"] = by.get(q.get("by") or "model", 0) + 1
    return {
        "method": method,
        "papers": n,
        "fields": total,
        "correct_pct": _pct(counts["correct"], total),
        "invented_pct": _pct(counts["invented"], total),
        "blank_pct": _pct(counts["blank"], total),
        "kind_correct_pct": _pct(sum(int(s["kind_ok"]) for s in scores), len(scores)),
        "tokens_total": tokens,
        "tokens_per_paper": round(tokens / n, 0) if n else 0,
        "seconds_per_paper": round(seconds / n, 1) if n else 0,
        "questions": len(qruns),
        "questions_match_pct": _pct(qmatch, len(qruns)),
        "questions_tokens_total": qtokens,
        "questions_by": by,
        "per_kind": {k: dict(v, correct_pct=_pct(v["correct"], v["correct"] + v["invented"] + v["blank"]),
                             invented_pct=_pct(v["invented"], v["correct"] + v["invented"] + v["blank"]),
                             blank_pct=_pct(v["blank"], v["correct"] + v["invented"] + v["blank"]))
                     for k, v in sorted(per_kind.items())},
        "per_field": dict(sorted(per_field.items())),
    }


def write_results(out_dir: str, summaries: dict[str, dict[str, Any]], details: dict[str, Any], model_name: str, tag: str = "") -> None:
    os.makedirs(out_dir, exist_ok=True)
    suffix = f"-{tag}" if tag else ""
    with open(os.path.join(out_dir, f"results{suffix}.json"), "w", encoding="utf-8") as fh:
        json.dump({"model": model_name, "date": date.today().isoformat(), "summary": summaries, "details": details}, fh, ensure_ascii=False, indent=1)
    label = {"baseline": "Baseline", "ours": "Ours"}
    lines = [
        "# Résultats : Apertus seul contre Apertus + code",
        "",
        f"Modèle (LLM_NAME) : `{model_name}` · date : {date.today().isoformat()} · papiers : "
        + ", ".join(f"{label[m]} {s['papers']}" for m, s in summaries.items()) + ".",
        "",
        "Champs comptés : montants, dates, IBAN, références, numéros (les clés de `schema.scored_keys`). "
        "« Inventé » = une valeur non vide qui diffère de la vérité (ou une valeur là où la vérité exige un blanc). "
        "Questions : la réponse contient le chiffre, la date ou la liste exacte attendue ; une réponse blanche "
        "(« je ne trouve pas ») ne compte juste que si la vérité attend un blanc ; seules les valeurs sont lues, jamais les raisons.",
        "",
        "| Setup | Metric | Result |",
        "|-------|--------|--------|",
    ]
    for m, s in summaries.items():
        L = label[m]
        lines += [
            f"| {L} | fields correct | {s['correct_pct']} % |",
            f"| {L} | fields invented (headline, lower is better) | {s['invented_pct']} % |",
            f"| {L} | fields blank | {s['blank_pct']} % |",
            f"| {L} | kind correct | {s['kind_correct_pct']} % |",
        ]
        if m == "baseline":
            lines.append(f"| Baseline (all texts in one prompt, context capped at {baseline.MAX_CONTEXT_CHARS} chars) | questions exact match ({s['questions']} q) | {s['questions_match_pct']} % |")
            if s.get("questions_retrieval"):
                lines.append(f"| Baseline + retrieval (same papers as Ours, raw text, free-form, no checks) | questions exact match ({s['questions_retrieval']} q) | {s['questions_retrieval_match_pct']} % |")
        else:
            by = s.get("questions_by") or {}
            path = "product path: ask.py rules first, model for the rest" if by.get("code") else "digest path"
            lines.append(f"| {L} | questions exact match ({s['questions']} q, {path}) | {s['questions_match_pct']} % |")
            if by:
                lines.append(f"| {L} | questions answered by rules / by model / none | {by.get('code', 0)} / {by.get('model', 0)} / {by.get('none', 0)} |")
        if s.get("questions_same_as_baseline"):
            lines.append(f"| {L} | questions exact match on the Baseline's {s['questions_same_as_baseline']} q | {s['questions_same_as_baseline_match_pct']} % |")
        lines += [
            f"| {L} | tokens per paper | {int(s['tokens_per_paper'])} |",
            f"| {L} | seconds per paper | {s['seconds_per_paper']} |",
        ]
    lines += ["", "## Par genre de papier", "", "| Kind | Setup | Papers | Correct | Invented | Blank |", "|------|-------|--------|---------|----------|-------|"]
    kinds = sorted({k for s in summaries.values() for k in s["per_kind"]})
    for k in kinds:
        for m, s in summaries.items():
            v = s["per_kind"].get(k)
            if v:
                lines.append(f"| {k} | {label[m]} | {v['papers']} | {v['correct_pct']} % | {v['invented_pct']} % | {v['blank_pct']} % |")
    lines += ["", "## Par champ (inventé / total)", "", "| Field | " + " | ".join(label[m] for m in summaries) + " |",
              "|-------|" + "|".join("-------" for _ in summaries) + "|"]
    fields = sorted({f for s in summaries.values() for f in s["per_field"]})
    for f in fields:
        cells = []
        for m, s in summaries.items():
            v = s["per_field"].get(f)
            cells.append(f"{v['invented']} / {v['correct'] + v['invented'] + v['blank']}" if v else "–")
        lines.append(f"| {f} | " + " | ".join(cells) + " |")
    lines.append("")
    with open(os.path.join(out_dir, f"results{suffix}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Baseline vs Ours sur les papiers générés")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--limit", type=int, default=0, help="n premiers papiers (0 = tous)")
    ap.add_argument("--methods", choices=["ours", "baseline", "both"], default="both")
    ap.add_argument("--questions", type=int, default=-1, help="n premières questions (-1 = toutes, 0 = aucune)")
    ap.add_argument("--baseline-questions", type=int, default=-1, help="n premières questions pour la Baseline (-1 = comme --questions)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--tag", default="", help="suffixe des résultats (docs/results-<tag>.*) et du cache (runtime/cache-<tag>/)")
    ap.add_argument("--no-new-calls", action="store_true", help="rejoue les contrôles sur le cache ; aucun appel au modèle")
    ap.add_argument("--ours-questions", choices=["product", "digest"], default="product",
                    help="product = ask.ask (règles puis modèle, le chemin du produit) ; digest = la sélection + résumé de eval.py")
    args = ap.parse_args(argv)
    global RUNTIME, NO_NEW_CALLS
    NO_NEW_CALLS = args.no_new_calls
    if NO_NEW_CALLS:
        llm.chat = _blocked_chat  # rien ne part vers le point d'accès pendant un rejeu
    if args.tag:
        RUNTIME = os.path.join(ROOT, "runtime", f"cache-{args.tag}")
    use_cache = not args.no_cache
    methods = ["baseline", "ours"] if args.methods == "both" else [args.methods]
    model_name = os.getenv("LLM_NAME") or llm.DEFAULT_MODEL
    if args.no_new_calls:
        # Le cache porte le nom du modèle dans son empreinte : on l'affiche tel quel.
        prev = os.path.join(args.out, f"results{'-' + args.tag if args.tag else ''}.json")
        if os.path.exists(prev):
            with open(prev, encoding="utf-8") as fh:
                model_name = json.load(fh).get("model", model_name)
        os.environ["LLM_NAME"] = model_name  # les empreintes du cache incluent ce nom

    truth_files = sorted(glob.glob(os.path.join(args.data, "truth", "*.json")))
    if args.limit:
        truth_files = truth_files[: args.limit]
    truths = [load_truth(p) for p in truth_files]
    if not truths:
        log(f"no truth files under {args.data}/truth")
        return 1
    pdfs = {t.id: os.path.join(args.data, "papers", os.path.basename(t.file) if t.file else t.id + ".pdf") for t in truths}
    missing = [t.id for t in truths if not os.path.exists(pdfs[t.id])]
    if missing:
        log(f"missing PDFs for {len(missing)} papers: {missing[:5]}")
        truths = [t for t in truths if t.id not in missing]

    summaries: dict[str, dict[str, Any]] = {}
    details: dict[str, Any] = {}
    questions = load_questions(os.path.join(args.data, "questions.json"))
    loaded_ids = {t.id for t in truths}
    if args.limit:
        questions = [q for q in questions if not q["papers"] or set(q["papers"]) <= loaded_ids]
    if args.questions >= 0:
        questions = questions[: args.questions]

    for method in methods:
        log(f"== {method}: {len(truths)} papers, {args.workers} workers")
        runs: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run_paper, method, t, pdfs[t.id], use_cache): t for t in truths}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    runs[t.id] = fut.result()
                except Exception as e:  # un papier qui casse ne doit pas casser la série
                    log(f"  {t.id}: ERROR {e}")
                    continue
                r = runs[t.id]
                log(f"  {t.id} {t.kind:16s} {r['seconds']:5.1f}s {r['tokens']['total_tokens']:6d} tok")
        ordered = [runs[t.id] for t in truths if t.id in runs]
        scores = [score_paper(runs[t.id]["paper"], t) for t in truths if t.id in runs]
        papers = [Paper.from_dict(r["paper"]) for r in ordered]
        texts = {p.id: p.text for p in papers}

        my_questions = questions
        if method == "baseline" and args.baseline_questions >= 0:
            my_questions = questions[: args.baseline_questions]

        def ask_all(qmethod: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            if not my_questions:
                return out
            log(f"== {qmethod}: {len(my_questions)} questions")
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs_q = {pool.submit(run_question, qmethod, q, papers, texts, use_cache): q for q in my_questions}
                for fut in as_completed(futs_q):
                    try:
                        qr = fut.result()
                    except Exception as e:
                        log(f"  question ERROR {e}")
                        continue
                    out.append(qr)
                    log(f"  {qr['id']} {'OK ' if qr['match'] else 'no '} {qr['answer'][:70]!r}")
            out.sort(key=lambda q: q["id"])
            return out

        qruns = ask_all("ours_product" if method == "ours" and args.ours_questions == "product" else method)
        summaries[method] = summarise(method, ordered, scores, qruns)
        details[method] = {"papers": scores, "questions": qruns,
                           "per_paper": {r["paper"]["id"]: {"tokens": r["tokens"], "seconds": r["seconds"], "kind": r["paper"]["kind"]} for r in ordered}}
        if method == "baseline":
            # Deuxième ligne de base pour les questions : les mêmes papiers que notre sélection, en texte brut.
            qr_runs = ask_all("baseline_retrieval")
            details[method]["questions_retrieval"] = qr_runs
            summaries[method]["questions_retrieval"] = len(qr_runs)
            summaries[method]["questions_retrieval_match_pct"] = _pct(sum(int(q["match"]) for q in qr_runs), len(qr_runs))
            summaries[method]["questions_retrieval_tokens_total"] = sum(q["tokens"]["total_tokens"] for q in qr_runs)
            log(f"== baseline_retrieval: questions {summaries[method]['questions_retrieval_match_pct']} %")
        s = summaries[method]
        log(f"== {method}: correct {s['correct_pct']} % · invented {s['invented_pct']} % · blank {s['blank_pct']} % · "
            f"questions {s['questions_match_pct']} % · {int(s['tokens_per_paper'])} tok/paper · {s['seconds_per_paper']} s/paper")

    # Ligne équitable : Ours sur les questions que la Baseline a vues.
    if "baseline" in summaries and "ours" in summaries:
        seen = {q["id"] for q in details["baseline"]["questions"]}
        same = [q for q in details["ours"]["questions"] if q["id"] in seen]
        summaries["ours"]["questions_same_as_baseline"] = len(same)
        summaries["ours"]["questions_same_as_baseline_match_pct"] = _pct(sum(int(q["match"]) for q in same), len(same))
    write_results(args.out, summaries, details, model_name, args.tag)
    suffix = f"-{args.tag}" if args.tag else ""
    log(f"wrote {os.path.join(args.out, f'results{suffix}.md')} and results{suffix}.json · model {model_name} · total tokens this run: {llm.usage_total()['total_tokens']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
