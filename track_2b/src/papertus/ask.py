"""Questions sur les papiers : d'abord des règles en code, sinon FTS5 + Apertus avec citations.

Ordre des opérations pour `ask(question, papers, lang)` :
1. `answer_from_facts` : quelques questions simples (somme due, solde à une date, nombre
   de papiers dans un mois, délai d'une autorité…) ont une réponse calculée par le code,
   sans appel au modèle. C'est cette voie que l'évaluation note.
2. Sinon, un index SQLite FTS5 (faits + texte) retient les six papiers les plus proches
   de la question. Rien trouvé → pas d'appel au modèle.
3. Sinon Apertus reçoit les faits de ces papiers (JSON compact, avec leurs ids) et doit
   répondre en citant [p0xx]. Chaque chiffre de la réponse est contrôlé contre les faits
   des papiers cités ; un chiffre introuvable ou une réponse sans citation → la réponse
   est remplacée par la ligne « je ne trouve pas ce chiffre ».
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from . import i18n
from .i18n import t
from .report import as_date, as_float, chf, field_triplet, field_value, fields_of, in_month, paper_attr

try:
    from . import verify as _verify
except ImportError:  # pragma: no cover
    _verify = None

TOP_K = 6

# Mots vides : on les retire de la question avant la recherche plein texte.
STOPWORDS: frozenset[str] = frozenset("""
le la les l un une des du de d et ou à a au aux en dans sur pour par avec sans ce cette ces cet
que qui quoi quel quelle quels quelles est sont ai as avons avez ont je tu il elle nous vous ils
elles me te se mon ma mes ton ta tes son sa ses notre votre leur leurs ne pas plus encore y
combien comment quand où dois doit dont été être avoir fait
der die das den dem des ein eine einer eines einem einen und oder zu zum zur in im am an auf
für von vom mit ohne ist sind bin bist habe hast hat haben hatte war waren ich du er sie es wir
ihr mein meine meinem meinen meiner dein sein seine ihre ihren ihrem unser noch nicht wie viel
viele wieviel welche welcher welches was wer wo wann muss soll schulde
il lo la i gli le un uno una di del della dei delle dello e o a al alla ai alle allo in nel nella
nei nelle con senza per su da dal dalla che chi cosa quale quali quanto quanta quanti quante è
sono ho hai ha abbiamo avete hanno mi ti si ci vi mio mia miei mie tuo tua suo sua nostro nostra
ancora non devo deve
the a an and or of to in on for with without is are am was were be been have has had my your his
her our their this that these those what which who how many much when where still not do does
""".split())

# Mots qui disent le genre de papier visé par la question (FR/DE/IT/EN).
BILL_WORDS = ("facture", "factures", "rechnung", "rechnungen", "fattura", "fatture", "bill", "bills", "invoice", "invoices")
LETTER_WORDS = ("lettre", "lettres", "brief", "briefe", "lettera", "lettere", "letter", "letters",
                "courrier", "schreiben", "décision", "verfügung", "decisione")
STATEMENT_WORDS = ("relevé", "relevés", "kontoauszug", "kontoauszüge", "auszug", "estratto", "estratti",
                   "statement", "statements")

# Mots des questions par type de règle.
COUNT_WORDS = ("combien de", "combien d'", "wie viele", "wieviele", "quante", "quanti", "how many")
TOTAL_WORDS = ("total", "somme", "summe", "totale", "montant total", "gesamtbetrag", "importo totale", "sum")
OWE_WORDS = ("dois-je", "dois je", "je dois", "doit-on", "encore à", "schulde", "noch an", "devo ancora", "devo a",
             "owe", "do i still", "still owe")
BALANCE_WORDS = ("solde", "saldo", "kontostand", "balance")
DUE_WORDS = ("fällig", "faellig", "échéance", "échéances", "échue", "échues", "échoit", "scad", "due", "payer", "zahlen", "pagare", "pay")
DEADLINE_WORDS = ("délai", "delai", "frist", "termine", "deadline", "entro quando", "bis wann", "jusqu'à quand")
UNTIL_WORDS = ("jusqu'au", "jusqu'à", "jusqu", "avant le", "d'ici", "bis", "bis zum", "bis am", "entro", "by", "before", "until")

# Institutions suisses : les mots qui les nomment dans les quatre langues, le genre de lettre
# qui leur correspond, et le genre de papier quand ce n'est pas une lettre (banque).
# Sert à la recherche (un papier porte les mots de sa classe dans toutes les langues) et
# aux règles (« combien dois-je encore à la caisse-maladie ? »).
INSTITUTIONS: dict[str, dict[str, Any]] = {
    "health": {"words": ("caisse-maladie", "caisse maladie", "assurance-maladie", "assurance maladie", "krankenkasse",
                         "krankenversicherung", "cassa malati", "health insurance", "health insurer"),
               "letter_kind": "premium_notice"},
    "debt": {"words": ("office des poursuites", "poursuites", "betreibungsamt", "betreibung", "ufficio esecuzioni",
                       "ufficio di esecuzione", "esecuzione", "debt collection"),
             "letter_kind": "payment_summons"},
    "commune": {"words": ("commune", "gemeinde", "comune", "municipality", "stadt", "ville", "città", "citta"),
                "letter_kind": "commune_notice"},
    "tax": {"words": ("impôts", "impots", "contributions", "steueramt", "steuerverwaltung", "steuer", "imposte", "tax"),
            "letter_kind": "tax_decision"},
    "permit": {"words": ("permis", "baubewilligung", "bauabteilung", "bauverwaltung", "permesso", "permessi", "urbanisme",
                         "constructions", "ufficio tecnico", "building permit"),
               "letter_kind": "permit_decision"},
    "bank": {"words": ("banque", "bank", "banca", "sparkasse", "cassa di risparmio", "caisse d'épargne", "caisse d'epargne"),
             "kind": "bank_statement"},
}


# Mots d'administration qui ne distinguent pas une autorité d'une autre (« Office », « Amt »…).
GENERIC_OFFICE_WORDS: frozenset[str] = frozenset("""
office ufficio amt service dienst administration amministrazione verwaltung kantonales kantonale kantonal
cantonal cantonale canton kanton cantone des del delle della dei der die das ag sa sarl gmbh
""".split())


def institution_in(question: str) -> str | None:
    """La classe d'institution nommée dans la question (le mot le plus long gagne), sinon None."""
    q = f" {fold(question)} "
    best, best_len = None, 0
    for cls, spec in INSTITUTIONS.items():
        for w in spec["words"]:
            fw = fold(w)
            if f" {fw} " in q and len(fw) > best_len:
                best, best_len = cls, len(fw)
    return best


def institution_classes(paper: Any) -> list[str]:
    """Les classes auxquelles un papier appartient : genre de lettre, ou mots de son autorité / émetteur / banque."""
    kind = paper_attr(paper, "kind")
    lk = field_value(paper, "letter_kind")
    names = " ".join(fold(field_value(paper, k) or "") for k in ("authority", "issuer", "bank"))
    out = []
    for cls, spec in INSTITUTIONS.items():
        if spec.get("kind") and spec["kind"] == kind:
            out.append(cls)
        elif spec.get("letter_kind") and lk == spec["letter_kind"]:
            out.append(cls)
        elif kind != "bank_statement" and any(f" {fold(w)} " in f" {names} " for w in spec["words"]):
            out.append(cls)
    return out


def papers_of_class(papers: list[Any], cls: str) -> list[Any]:
    return [p for p in papers if cls in institution_classes(p)]


_MONTH_INDEX: dict[str, int] = {}
for _lang, _names in i18n.MONTH_NAMES.items():
    for _i, _n in enumerate(_names, 1):
        _MONTH_INDEX[_n.lower()] = _i
_MONTH_INDEX.update({"fevrier": 2, "aout": 8, "decembre": 12, "maerz": 3, "marz": 3})


# ---------------------------------------------------------------------------
# Normalisation de texte
# ---------------------------------------------------------------------------

def fold(s: str) -> str:
    """Minuscules, sans accents, sans ponctuation, espaces simples."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("ß", "ss")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [w for w in fold(s).split() if w]


def query_terms(question: str) -> list[str]:
    """Mots porteurs de la question : sans mots vides, nombres et noms gardés."""
    out: list[str] = []
    for w in tokens(question):
        if w in STOPWORDS:
            continue
        if len(w) < 2 and not w.isdigit():
            continue
        out.append(w)
    return out


def _date_spellings(d: date) -> list[str]:
    out = [d.isoformat(), f"{d.day:02d}.{d.month:02d}.{d.year}", f"{d.day}.{d.month}.{d.year}"]
    for lang in i18n.LANGS:
        out.append(f"{d.day} {i18n.MONTH_NAMES[lang][d.month - 1]} {d.year}")
    return out


def _amount_spellings(v: float) -> list[str]:
    return [f"{v:.2f}", chf(v), str(int(v)) if v == int(v) else f"{v:.2f}"]


# ---------------------------------------------------------------------------
# L'index FTS5
# ---------------------------------------------------------------------------

def paper_document(paper: Any) -> tuple[str, str]:
    """(faits, texte) indexables d'un papier : valeurs nues, dates et montants en plusieurs écritures."""
    kind = paper_attr(paper, "kind", "")
    parts: list[str] = [kind.replace("_", " ")]
    parts += {"qr_bill": list(BILL_WORDS), "official_letter": list(LETTER_WORDS),
              "bank_statement": list(STATEMENT_WORDS)}.get(kind, [])
    for cls in institution_classes(paper):  # les mots de la classe dans les quatre langues
        parts += list(INSTITUTIONS[cls]["words"])
    for key, f in fields_of(paper).items():
        v = f.get("value") if isinstance(f, dict) else getattr(f, "value", None)
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, list):
            for m in v:
                if isinstance(m, dict):
                    parts.append(" ".join(str(x) for x in m.values() if x is not None))
                    d = as_date(m.get("date"))
                    if d:
                        parts += _date_spellings(d)
            continue
        d = as_date(v)
        if d:
            parts += _date_spellings(d)
            continue
        if isinstance(v, (int, float)):
            parts += _amount_spellings(float(v))
            continue
        parts.append(str(v))
    return " \n".join(parts), str(paper_attr(paper, "text", "") or "")


class Index:
    """Index plein texte SQLite FTS5 ; en mémoire par défaut, ou sur disque (path)."""

    def __init__(self, path: str | None = None) -> None:
        self.conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS papers USING fts5("
                          "id UNINDEXED, kind UNINDEXED, lang UNINDEXED, facts, text, "
                          "tokenize='unicode61 remove_diacritics 2')")
        self.conn.commit()

    def clear(self) -> None:
        self.conn.execute("DELETE FROM papers")
        self.conn.commit()

    def add(self, paper: Any) -> None:
        pid = paper_attr(paper, "id", "")
        self.conn.execute("DELETE FROM papers WHERE id = ?", (pid,))
        facts, text = paper_document(paper)
        self.conn.execute("INSERT INTO papers (id, kind, lang, facts, text) VALUES (?, ?, ?, ?, ?)",
                          (pid, paper_attr(paper, "kind", ""), paper_attr(paper, "language", ""), facts, text))
        self.conn.commit()

    def build(self, papers: list[Any]) -> None:
        self.clear()
        for p in papers:
            self.add(p)

    def search(self, question: str, limit: int = TOP_K) -> list[tuple[str, float]]:
        """ids des papiers les plus proches, du meilleur au moins bon (bm25, faits pesant double)."""
        terms = query_terms(question)
        cls = institution_in(question)
        if cls:  # « caisse-maladie » doit aussi trouver « Krankenkasse » et « cassa malati »
            for w in INSTITUTIONS[cls]["words"]:
                terms += [x for x in query_terms(w) if x not in terms]
        phrases = [f'"{d.isoformat()}"' for d in _dates_in(question)]
        if not terms and not phrases:
            return []
        q = " OR ".join(phrases + [f'"{w}"' for w in terms])
        try:
            rows = self.conn.execute(
                "SELECT id, bm25(papers, 0, 0, 0, 2.0, 1.0) AS s FROM papers WHERE papers MATCH ? "
                "ORDER BY s LIMIT ?", (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r[0], float(r[1])) for r in rows]


def build_index(papers: list[Any], path: str | None = None) -> Index:
    idx = Index(path)
    idx.build(papers)
    return idx


# ---------------------------------------------------------------------------
# Lecture de la question : dates, mois, noms
# ---------------------------------------------------------------------------

def _dates_in(question: str) -> list[date]:
    found: set[date] = set()
    if _verify is not None and hasattr(_verify, "dates_in_text"):
        try:
            found |= set(_verify.dates_in_text(question))
        except Exception:
            pass
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", question):
        try:
            found.add(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", question):
        try:
            found.add(date(int(m[3]), int(m[2]), int(m[1])))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})(?:er|\.|º|°)?\s+([^\W\d_]+)\s+(\d{4})\b", question):
        mo = _MONTH_INDEX.get(fold(m.group(2)))
        if mo:
            try:
                found.add(date(int(m.group(3)), mo, int(m.group(1))))
            except ValueError:
                pass
    return sorted(found)


def _month_in(question: str, papers: list[Any]) -> str | None:
    """'en septembre' / 'im September 2026' → '2026-09' ; sans année → l'année la plus fréquente des papiers."""
    folded = fold(question)
    words = folded.split()
    mo = None
    for w in words:
        if w in _MONTH_INDEX:
            mo = _MONTH_INDEX[w]
            break
    if mo is None:
        m = re.search(r"\b(\d{4})-(\d{2})\b", question)
        return m.group(0) if m else None
    years = re.findall(r"\b(20\d{2})\b", question)
    year = int(years[0]) if years else _common_year(papers)
    return f"{year}-{mo:02d}"


def _common_year(papers: list[Any]) -> int:
    counts: dict[int, int] = {}
    for p in papers:
        for key in ("invoice_date", "due_date", "period_end", "letter_date", "deadline"):
            d = as_date(field_value(p, key))
            if d:
                counts[d.year] = counts.get(d.year, 0) + 1
    return max(counts, key=counts.get) if counts else date.today().year


def _name_matches(question_folded: str, name: str | None) -> int:
    """Score de présence d'un nom dans la question : longueur du nom s'il y est entier,
    sinon nombre de mots significatifs retrouvés (≥ 2 exigés), sinon 0."""
    if not name:
        return 0
    n = fold(name)
    if not n:
        return 0
    if f" {n} " in f" {question_folded} ":
        return 100 + len(n)
    words = [w for w in n.split() if len(w) >= 4 and w not in STOPWORDS]
    hits = sum(1 for w in words if f" {w} " in f" {question_folded} ")
    if words and hits == len(words) and hits >= 1:
        return 50 + hits
    return hits if hits >= 2 else 0


def _best_named(question: str, papers: list[Any], kind: str, key: str) -> tuple[str | None, list[Any]]:
    """Le nom (émetteur / autorité) le mieux présent dans la question et les papiers qui le portent."""
    q = fold(question)
    best_name, best_score = None, 0
    for p in papers:
        if paper_attr(p, "kind") != kind:
            continue
        name = field_value(p, key)
        score = _name_matches(q, name)
        if score > best_score:
            best_name, best_score = name, score
    if not best_name:
        return None, []
    target = fold(best_name)
    hits = [p for p in papers if paper_attr(p, "kind") == kind and fold(field_value(p, key) or "") == target]
    return best_name, hits


_NUMBER_TOKEN = re.compile(r"\b\d{4}-\d{2,6}(?:-[A-Z]{2})?\b|\b[A-Z]{1,3}-?\d{3,}\b|\b\d{5,}\b")


def _ids_in(question: str) -> list[str]:
    """Numéros de facture ou de référence écrits dans la question (2026-994, 2026-48829-GE…)."""
    return [m.group(0) for m in _NUMBER_TOKEN.finditer(question)]


_DATE_SPANS = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\.?\s+[^\W\d_]+\s+\d{4}\b")


def _account_suffix(question: str) -> str | None:
    """Les derniers chiffres d'un compte cités dans la question (« compte se terminant par 5840 »,
    « Konto endend auf 2449 ») : un groupe de 4 chiffres ou plus qui ne fait pas partie d'une date."""
    rest = _DATE_SPANS.sub(" ", question)
    found = re.findall(r"(?<![\d-])\d{4,}(?![\d-])", rest)
    return found[-1] if found else None


def _with_id(papers: list[Any], key: str, ids: list[str]) -> list[Any]:
    """Les papiers dont le champ `key` est l'un des numéros donnés (comparaison sans espaces, sans casse)."""
    wanted = {re.sub(r"\s+", "", i).lower() for i in ids}
    out = []
    for p in papers:
        v = field_value(p, key)
        if v is not None and re.sub(r"\s+", "", str(v)).lower() in wanted:
            out.append(p)
    return out


def _any(question_folded: str, words: tuple[str, ...]) -> bool:
    return any(fold(w) in question_folded for w in words)


# ---------------------------------------------------------------------------
# Réponses calculées par le code
# ---------------------------------------------------------------------------

@dataclass
class Answer:
    text: str
    cited_ids: list[str] = field(default_factory=list)
    grounded: bool = False
    note: str = ""
    value: Any = None          # la valeur nue (nombre, date ISO, liste d'ids) quand le code l'a calculée
    by: str = "none"           # "code" | "model" | "none"
    model_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt_date(d: date | str | None, lang: str) -> str:
    """Date à la suisse, jj.mm.aaaa : la même dans les quatre langues."""
    dd = as_date(d)
    if not dd:
        return "—"
    return f"{dd.day:02d}.{dd.month:02d}.{dd.year}"


def answer_from_facts(question: str, papers: list[Any], lang: str = "fr") -> Answer | None:
    """Règles déterministes ; None quand aucune ne s'applique (le modèle prend alors le relais)."""
    lang = i18n.norm_lang(lang)
    q = fold(question)
    dates = _dates_in(question)
    month = _month_in(question, papers)
    bills = [p for p in papers if paper_attr(p, "kind") == "qr_bill"]

    cls = institution_in(question)

    # 0. Une classe d'institution (caisse-maladie, Office des poursuites, commune, impôts…)
    if cls and not _ids_in(question):
        narrowed = _narrow_to_named(question, papers_of_class(papers, cls), cls)
        if narrowed and _any(q, DEADLINE_WORDS):
            a = _class_deadline(narrowed, cls, lang)
            if a:
                return a
        if narrowed and (_any(q, OWE_WORDS) or _any(q, ("combien", "wie viel", "quanto", "how much"))) and not month:
            a = _class_owed(narrowed, cls, lang)
            if a:
                return a

    # 1. Délai d'une autorité : « Quel est le délai de {authority} ? »
    if _any(q, DEADLINE_WORDS):
        letters = [p for p in papers if paper_attr(p, "kind") == "official_letter"]
        hits = _with_id(letters, "reference_number", _ids_in(question))
        authority = field_value(hits[0], "authority") if hits else None
        if not hits:
            authority, hits = _best_named(question, papers, "official_letter", "authority")
        if hits:
            dated = [(as_date(field_value(p, "deadline")), p) for p in hits]
            dated = [(d, p) for d, p in dated if d]
            if dated:
                d, p = max(dated)  # le délai le plus récent de cette autorité
                return Answer(text=t("ask.answer.deadline", lang, authority=authority, date=_fmt_date(d, lang)),
                              cited_ids=[paper_attr(p, "id")], grounded=True, value=d.isoformat(), by="code")

    # 2. Ce que je dois encore à un émetteur : « Combien dois-je encore à {issuer} ? »
    if _any(q, OWE_WORDS) or (_any(q, ("combien", "wie viel", "quanto", "how much")) and not month and not dates):
        issuer, hits = _best_named(question, papers, "qr_bill", "issuer")
        by_number = _with_id(bills, "invoice_number", _ids_in(question))
        if by_number:
            narrowed = [p for p in hits if p in by_number]
            hits = narrowed or by_number
            issuer = issuer or field_value(hits[0], "issuer")
        if hits:
            amounts = [as_float(field_value(p, "amount_chf")) for p in hits]
            known = [a for a in amounts if a is not None]
            total = round(sum(known), 2)
            return Answer(text=t("ask.answer.owed", lang, issuer=issuer, total=chf(total), n=len(hits)),
                          cited_ids=[paper_attr(p, "id") for p in hits], grounded=True, value=total, by="code")

    # 3. Solde d'un compte à une date : « Quel est le solde du compte au {period_end} ? »
    if _any(q, BALANCE_WORDS) and dates:
        target = dates[-1]
        hits = [p for p in papers if paper_attr(p, "kind") == "bank_statement"
                and as_date(field_value(p, "period_end")) == target]
        bank, named = _best_named(question, hits, "bank_statement", "bank")
        if named:
            hits = named
        suffix = _account_suffix(question)
        if suffix:  # « compte se terminant par 5840 » : un seul compte, une seule valeur
            by_suffix = [p for p in hits if re.sub(r"\s+", "", str(field_value(p, "account_iban") or "")).endswith(suffix)]
            if by_suffix:
                hits = by_suffix[:1]
        if hits:
            lines, ids, values, grounded = [], [], [], True
            for p in hits:
                closing, status, reason = field_triplet(p, "closing_balance")
                account = field_value(p, "bank") or field_value(p, "account_iban") or paper_attr(p, "id")
                v = as_float(closing)
                ids.append(paper_attr(p, "id"))
                if v is None:
                    lines.append(t("ask.answer.balance_blank", lang, account=account, date=_fmt_date(target, lang),
                                   reason=reason or "blank"))
                    values.append(None)
                else:
                    lines.append(t("ask.answer.balance", lang, account=account, date=_fmt_date(target, lang), amount=chf(v)))
                    values.append(v)
            value = values[0] if len(values) == 1 else values
            return Answer(text=" ".join(lines), cited_ids=ids, grounded=True, value=value, by="code")

    # 4. Factures échues : liste à une date, ou somme jusqu'à une date
    if _any(q, DUE_WORDS) and dates:
        target = dates[-1]
        if _any(q, TOTAL_WORDS) or _any(q, UNTIL_WORDS) or _any(q, ("combien", "wie viel", "quanto", "how much")):
            hits = [p for p in bills if (d := as_date(field_value(p, "due_date"))) and d <= target]
            total = round(sum(as_float(field_value(p, "amount_chf")) or 0.0 for p in hits), 2)
            return Answer(text=t("ask.answer.sum_due", lang, date=_fmt_date(target, lang), n=len(hits), total=chf(total)),
                          cited_ids=[paper_attr(p, "id") for p in hits], grounded=True, value=total, by="code")
        hits = [p for p in bills if as_date(field_value(p, "due_date")) == target]
        ids = [paper_attr(p, "id") for p in hits]
        if hits:
            desc = ", ".join(f"[{paper_attr(p, 'id')}] {field_value(p, 'issuer') or ''} CHF {chf(as_float(field_value(p, 'amount_chf')))}"
                             for p in hits)
            text = t("ask.answer.due_on", lang, date=_fmt_date(target, lang), ids=desc)
        else:
            text = t("ask.answer.none_due", lang, date=_fmt_date(target, lang))
        return Answer(text=text, cited_ids=ids, grounded=True, value=ids, by="code")

    # 5. Nombre de papiers dans un mois : « Quante fatture ho ricevuto in settembre? »
    if month and _any(q, COUNT_WORDS):
        kind, key, what = _kind_from(q)
        hits = [p for p in papers if (kind is None or paper_attr(p, "kind") == kind)
                and in_month(as_date(field_value(p, key if kind else _date_key(p))), month)]
        return Answer(text=t("ask.answer.count_month", lang, n=len(hits), what=t(what, lang),
                             month=i18n.month_label(month, lang)),
                      cited_ids=[paper_attr(p, "id") for p in hits], grounded=True, value=len(hits), by="code")

    # 6. Total des factures reçues dans un mois
    if month and (_any(q, TOTAL_WORDS) or _any(q, ("combien", "wie viel", "quanto", "how much"))):
        hits = [p for p in bills if in_month(as_date(field_value(p, "invoice_date")), month)]
        total = round(sum(as_float(field_value(p, "amount_chf")) or 0.0 for p in hits), 2)
        return Answer(text=t("ask.answer.sum_month", lang, month=i18n.month_label(month, lang), n=len(hits), total=chf(total)),
                      cited_ids=[paper_attr(p, "id") for p in hits], grounded=True, value=total, by="code")

    return None


def _narrow_to_named(question: str, hits: list[Any], cls: str) -> list[Any]:
    """Si la question nomme une autorité précise de la classe (« Office des poursuites de la Riviera »),
    on ne garde qu'elle ; un nom qui n'est que le mot générique de la classe ne restreint pas."""
    q = fold(question)
    class_words = {w for word in INSTITUTIONS[cls]["words"] for w in fold(word).split()} | GENERIC_OFFICE_WORDS
    named = []
    for p in hits:
        name = field_value(p, "authority") or field_value(p, "issuer") or field_value(p, "bank") or ""
        fn = fold(name)
        extra = {w for w in fn.split() if w not in class_words and w not in STOPWORDS and len(w) >= 3}
        if fn and f" {fn} " in f" {q} " and extra:
            named.append(p)
    return named or hits


def _who(p: Any) -> str:
    return str(field_value(p, "authority") or field_value(p, "issuer") or field_value(p, "bank") or paper_attr(p, "id"))


def _class_owed(hits: list[Any], cls: str, lang: str) -> Answer | None:
    """Somme des montants des papiers de la classe qui portent un délai ou une échéance, chacun cité."""
    dated = [p for p in hits if as_date(field_value(p, "deadline")) or as_date(field_value(p, "due_date"))]
    if not dated:
        return None
    with_amount = [p for p in dated if as_float(field_value(p, "amount_chf")) is not None]
    inst = t(f"inst.{cls}", lang)
    if not with_amount:
        items = ", ".join(f"[{paper_attr(p, 'id')}] {_who(p)}" for p in dated)
        return Answer(text=t("ask.answer.owed_class_none", lang, inst=inst, n=len(dated), items=items),
                      cited_ids=[paper_attr(p, "id") for p in dated], grounded=True, value=0.0, by="code")
    with_amount.sort(key=lambda p: (field_value(p, "deadline") or field_value(p, "due_date") or "", paper_attr(p, "id")))
    total = round(sum(as_float(field_value(p, "amount_chf")) for p in with_amount), 2)
    items = ", ".join(
        f"[{paper_attr(p, 'id')}] {_who(p)} CHF {chf(as_float(field_value(p, 'amount_chf')))} · "
        f"{t('ask.item.deadline', lang)} {_fmt_date(field_value(p, 'deadline') or field_value(p, 'due_date'), lang)}"
        for p in with_amount)
    return Answer(text=t("ask.answer.owed_class", lang, inst=inst, total=chf(total), n=len(with_amount), items=items),
                  cited_ids=[paper_attr(p, "id") for p in with_amount], grounded=True, value=total, by="code")


def _class_deadline(hits: list[Any], cls: str, lang: str) -> Answer | None:
    """Le prochain délai à venir parmi les papiers de la classe (sinon le dernier passé), tous cités."""
    dated = [(as_date(field_value(p, "deadline")), p) for p in hits]
    dated = sorted(((d, p) for d, p in dated if d), key=lambda x: (x[0], paper_attr(x[1], "id")))
    if not dated:
        return None
    today = date.today()
    future = [(d, p) for d, p in dated if d >= today]
    d, p = future[0] if future else dated[-1]
    inst = t(f"inst.{cls}", lang)
    key = "ask.answer.deadline_class" if future else "ask.answer.deadline_class_past"
    text = t(key, lang, inst=inst, date=_fmt_date(d, lang), who=_who(p), pid=paper_attr(p, "id"))
    others = [(dd, pp) for dd, pp in dated if pp is not p]
    if others:
        text += " " + t("ask.answer.deadline_others", lang,
                        items=", ".join(f"[{paper_attr(pp, 'id')}] {_fmt_date(dd, lang)}" for dd, pp in others))
    return Answer(text=text, cited_ids=[paper_attr(p, "id")] + [paper_attr(pp, "id") for _, pp in others],
                  grounded=True, value=d.isoformat(), by="code")


def _kind_from(q: str) -> tuple[str | None, str, str]:
    if _any(q, BILL_WORDS):
        return "qr_bill", "invoice_date", "what.bills"
    if _any(q, LETTER_WORDS):
        return "official_letter", "letter_date", "what.letters"
    if _any(q, STATEMENT_WORDS):
        return "bank_statement", "period_end", "what.statements"
    return None, "", "what.papers"


def _date_key(p: Any) -> str:
    return {"qr_bill": "invoice_date", "official_letter": "letter_date", "bank_statement": "period_end"}.get(
        paper_attr(p, "kind"), "invoice_date")


# ---------------------------------------------------------------------------
# La voie du modèle
# ---------------------------------------------------------------------------

_CITE = re.compile(r"\[(p\d{3,})\]")
_LANG_NAME = {"fr": "French", "de": "Swiss Standard German (never ß, write ss)", "it": "Italian", "en": "English"}


def compact_facts(paper: Any) -> dict[str, Any]:
    """Faits nus d'un papier (sans les blancs), pour le modèle."""
    plain: dict[str, Any] = {}
    for k, f in fields_of(paper).items():
        v = f.get("value") if isinstance(f, dict) else getattr(f, "value", None)
        if v is None or k == "balanced":
            continue
        if isinstance(v, list):
            v = v[:40]
        plain[k] = v
    return {"id": paper_attr(paper, "id"), "kind": paper_attr(paper, "kind"),
            "language": paper_attr(paper, "language"), "facts": plain}


def ask_messages(question: str, candidates: list[Any], lang: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": (
            f"You answer questions about someone's Swiss papers, in {_LANG_NAME.get(lang, 'French')}, in one or two "
            "sentences. You receive the facts of a few papers as JSON, each with an id. Every fact you use must be "
            "followed by its paper id in square brackets, like [p012]. Use only the numbers and dates written in "
            "the facts; never compute, convert or round. If the facts do not answer the question, say so in one "
            "sentence without any number. No markdown.")},
        {"role": "user", "content": json.dumps({"question": question, "papers": [compact_facts(p) for p in candidates]},
                                              ensure_ascii=False)},
    ]


def _grounded(text: str, facts: Any) -> bool:
    if _verify is not None and hasattr(_verify, "numbers_grounded"):
        try:
            res = _verify.numbers_grounded(text, facts)
            return bool(res[0] if isinstance(res, tuple) else res)
        except Exception:
            return False
    from .report import allowed_numbers, _local_grounded
    return _local_grounded(text, allowed_numbers(facts))


def ask(question: str, papers: list[Any], lang: str = "fr", index: Index | None = None) -> Answer:
    """Règles → FTS5 → Apertus avec citations ; chaque chiffre contrôlé."""
    lang = i18n.norm_lang(lang)
    question = (question or "").strip()
    if not question:
        return Answer(text=t("ask.no_paper", lang), by="none")

    ruled = answer_from_facts(question, papers, lang)
    if ruled is not None:
        return ruled

    idx = index or build_index(papers)
    hits = idx.search(question, TOP_K)
    if not hits:
        return Answer(text=t("ask.no_paper", lang), by="none")
    by_id = {paper_attr(p, "id"): p for p in papers}
    candidates = [by_id[i] for i, _ in hits if i in by_id]
    if not candidates:
        return Answer(text=t("ask.no_paper", lang), by="none")

    from . import llm
    try:
        text, usage = llm.chat(ask_messages(question, candidates, lang), json=False, max_tokens=300, temperature=0)
    except Exception as e:
        return Answer(text=t("ask.not_found", lang), cited_ids=[c["id"] for c in map(compact_facts, candidates)],
                      grounded=False, note=f"{t('ask.note.model_silent', lang)} ({type(e).__name__})", by="model")
    tokens_used = int(usage.get("total_tokens", 0))
    text = (text or "").strip()
    cited = [i for i in dict.fromkeys(_CITE.findall(text)) if i in by_id]
    if text and not cited:
        # Une seule reprise, plus stricte : au moins un id de la liste entre crochets.
        strict = ask_messages(question, candidates, lang)
        strict[0]["content"] += (" You MUST cite at least one paper id from the list, in square brackets like [p012], "
                                 "after the fact it supports. An answer without any [p0xx] is rejected.")
        try:
            text2, usage2 = llm.chat(strict, json=False, max_tokens=300, temperature=0)
            tokens_used += int(usage2.get("total_tokens", 0))
            text2 = (text2 or "").strip()
            cited2 = [i for i in dict.fromkeys(_CITE.findall(text2)) if i in by_id]
            if cited2:
                text, cited = text2, cited2
        except Exception:
            pass
    if not text:
        return Answer(text=t("ask.not_found", lang), grounded=False, note=t("ask.note.model_silent", lang),
                      by="model", model_tokens=tokens_used)
    if not cited:
        return Answer(text=t("ask.not_found", lang), grounded=False, note=t("ask.note.no_citation", lang),
                      by="model", model_tokens=tokens_used)
    facts = [compact_facts(by_id[i])["facts"] for i in cited]
    if not _grounded(_CITE.sub(" ", text), facts):
        return Answer(text=t("ask.not_found", lang), cited_ids=cited, grounded=False,
                      note=t("ask.note.ungrounded", lang), by="model", model_tokens=tokens_used)
    return Answer(text=text, cited_ids=cited, grounded=True, by="model", model_tokens=tokens_used)
