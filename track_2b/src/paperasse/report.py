"""Rapport mensuel : des tableaux construits par le code, puis un court paragraphe
rédigé par Apertus à partir de ces tableaux, dont chaque chiffre est contrôlé.

Règle du projet : le code fait l'arithmétique, le modèle fait la langue. Une phrase
dont un chiffre n'apparaît pas dans les tableaux est retirée et une note le dit.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from . import i18n
from .i18n import t

try:  # verify.py est écrit par un autre binôme ; en son absence, un contrôle local équivalent.
    from . import verify as _verify
except ImportError:  # pragma: no cover - dépend de l'ordre d'arrivée des fichiers
    _verify = None

TOLERANCE = 0.01  # centime
PROSE_MAX_WORDS = 80


# ---------------------------------------------------------------------------
# Lecture tolérante des faits (Field dataclass ou dict JSON)
# ---------------------------------------------------------------------------

def fields_of(paper: Any) -> dict[str, Any]:
    """Les champs d'un papier : Facts.fields, ou le dict JSON plat {clé: champ}."""
    facts = getattr(paper, "facts", None)
    if facts is None and isinstance(paper, dict):
        facts = paper.get("facts")
    fields = getattr(facts, "fields", None)
    if fields is None and isinstance(facts, dict):
        fields = facts.get("fields") if isinstance(facts.get("fields"), dict) else facts
    return fields or {}


def field_triplet(paper: Any, key: str) -> tuple[Any, str, str | None]:
    """(valeur, statut, raison) d'un champ ; champ absent → (None, 'blank', 'missing')."""
    f = fields_of(paper).get(key)
    if f is None:
        return None, "blank", "missing"
    if isinstance(f, dict):
        return f.get("value"), f.get("status") or "blank", f.get("reason")
    return getattr(f, "value", None), getattr(f, "status", "blank") or "blank", getattr(f, "reason", None)


def field_value(paper: Any, key: str) -> Any:
    return field_triplet(paper, key)[0]


def paper_attr(paper: Any, key: str, default: Any = None) -> Any:
    if isinstance(paper, dict):
        return paper.get(key, default)
    return getattr(paper, key, default)


def as_float(v: Any) -> float | None:
    """Nombre → float ; chaîne suisse « 1'234.50 » / « 1 234.50 » / « 1234.— » → float ; sinon None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("'", "").replace("’", "").replace(" ", "").replace(" ", "")
    s = s.replace(".—", ".00").replace(".-", ".00").replace(",", ".")
    s = re.sub(r"^(CHF|Fr\.?)", "", s, flags=re.I)
    try:
        return float(s)
    except ValueError:
        return None


def as_date(v: Any) -> date | None:
    """ISO 'YYYY-MM-DD' (ou 'DD.MM.YYYY') → date ; sinon None."""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def month_bounds(month: str) -> tuple[date, date]:
    """'2026-09' → (1er, dernier jour)."""
    y, m = (int(x) for x in month.split("-")[:2])
    first = date(y, m, 1)
    last = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, date.fromordinal(last.toordinal() - 1)


def in_month(d: date | None, month: str) -> bool:
    return d is not None and d.strftime("%Y-%m") == month


def chf(v: float | None) -> str:
    """Format suisse : 1'234.50 ; None → tiret."""
    if v is None:
        return "—"
    s = f"{abs(v):,.2f}".replace(",", "'")
    return ("-" if v < 0 else "") + s


# ---------------------------------------------------------------------------
# Les tableaux
# ---------------------------------------------------------------------------

@dataclass
class BillRow:
    paper_id: str
    issuer: str | None
    invoice_date: str | None
    due_date: str | None
    amount: float | None
    state: str  # "to_pay" | "unknown"
    amount_status: str = "blank"


@dataclass
class AccountRow:
    paper_id: str
    account: str | None
    bank: str | None
    period_end: str | None
    opening: float | None
    inflow: float
    outflow: float
    closing: float | None
    balanced: bool
    closing_reason: str | None = None


@dataclass
class LetterRow:
    paper_id: str
    authority: str | None
    subject: str | None
    deadline: str | None
    amount: float | None


@dataclass
class Report:
    month: str
    lang: str
    bills: list[BillRow] = field(default_factory=list)
    accounts: list[AccountRow] = field(default_factory=list)
    letters: list[LetterRow] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)
    prose: str = ""
    notes: list[str] = field(default_factory=list)
    removed_sentences: int = 0
    model_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _iso(v: Any) -> str | None:
    d = as_date(v)
    return d.isoformat() if d else _str_or_none(v)


def bill_rows(papers: list[Any], month: str) -> list[BillRow]:
    """Factures QR du mois : date de facture dans le mois (sinon échéance dans le mois)."""
    first, _ = month_bounds(month)
    rows: list[BillRow] = []
    for p in papers:
        if paper_attr(p, "kind") != "qr_bill":
            continue
        inv = as_date(field_value(p, "invoice_date"))
        due = as_date(field_value(p, "due_date"))
        if not (in_month(inv, month) or (inv is None and in_month(due, month))):
            continue
        amount, status, _ = field_triplet(p, "amount_chf")
        amount_f = as_float(amount)
        # Payé ou non : on ne le sait pas. « À payer » seulement si l'échéance est dans ou après le mois.
        state = "to_pay" if (due is not None and due >= first) else "unknown"
        rows.append(BillRow(
            paper_id=paper_attr(p, "id", ""),
            issuer=_str_or_none(field_value(p, "issuer")),
            invoice_date=_iso(inv),
            due_date=_iso(due),
            amount=amount_f,
            state=state,
            amount_status=status,
        ))
    rows.sort(key=lambda r: (r.due_date or "9999", r.paper_id))
    return rows


def movements_in_out(movs: Any) -> tuple[float, float, float]:
    """(entrées, sorties, somme signée) des mouvements ; tolère liste de dicts ou d'objets."""
    inflow = outflow = 0.0
    for m in movs or []:
        a = as_float(m.get("amount") if isinstance(m, dict) else getattr(m, "amount", None))
        if a is None:
            continue
        if a >= 0:
            inflow += a
        else:
            outflow += -a
    return round(inflow, 2), round(outflow, 2), round(inflow - outflow, 2)


def account_rows(papers: list[Any], month: str) -> list[AccountRow]:
    """Relevés dont la fin de période tombe dans le mois (sinon le début)."""
    rows: list[AccountRow] = []
    for p in papers:
        if paper_attr(p, "kind") != "bank_statement":
            continue
        end = as_date(field_value(p, "period_end"))
        start = as_date(field_value(p, "period_start"))
        if not (in_month(end, month) or (end is None and in_month(start, month))):
            continue
        opening = as_float(field_value(p, "opening_balance"))
        closing, closing_status, closing_reason = field_triplet(p, "closing_balance")
        closing_f = as_float(closing)
        inflow, outflow, net = movements_in_out(field_value(p, "movements"))
        # Le code refait la somme : ouverture + mouvements doit tomber sur la clôture.
        balanced = (
            opening is not None and closing_f is not None
            and abs(round(opening + net, 2) - closing_f) <= TOLERANCE
        )
        if closing_f is not None and not balanced:
            closing_reason = closing_reason or "arithmetic"
        rows.append(AccountRow(
            paper_id=paper_attr(p, "id", ""),
            account=_str_or_none(field_value(p, "account_iban")),
            bank=_str_or_none(field_value(p, "bank")),
            period_end=_iso(end),
            opening=opening,
            inflow=inflow,
            outflow=outflow,
            closing=closing_f if balanced else None,
            balanced=balanced,
            closing_reason=None if balanced else (closing_reason or "arithmetic"),
        ))
    rows.sort(key=lambda r: (r.account or "", r.paper_id))
    return rows


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"true", "yes", "oui", "ja", "sì", "si", "1", "x"}


def letter_rows(papers: list[Any], month: str) -> list[LetterRow]:
    """Lettres officielles du mois qui demandent une action (ou qui portent un délai)."""
    rows: list[LetterRow] = []
    for p in papers:
        if paper_attr(p, "kind") != "official_letter":
            continue
        ld = as_date(field_value(p, "letter_date"))
        deadline = as_date(field_value(p, "deadline"))
        if not (in_month(ld, month) or in_month(deadline, month)):
            continue
        action = field_value(p, "action_required")
        if not (_truthy(action) or deadline is not None):
            continue
        rows.append(LetterRow(
            paper_id=paper_attr(p, "id", ""),
            authority=_str_or_none(field_value(p, "authority")),
            subject=_str_or_none(field_value(p, "subject")),
            deadline=_iso(deadline),
            amount=as_float(field_value(p, "amount_chf")),
        ))
    rows.sort(key=lambda r: (r.deadline or "9999", r.paper_id))
    return rows


def compute_totals(bills: list[BillRow], accounts: list[AccountRow], letters: list[LetterRow]) -> dict[str, float]:
    return {
        "bills_count": float(len(bills)),
        "bills_total": round(sum(r.amount or 0.0 for r in bills), 2),
        "bills_to_pay": round(sum(r.amount or 0.0 for r in bills if r.state == "to_pay"), 2),
        "accounts_count": float(len(accounts)),
        "inflow_total": round(sum(r.inflow for r in accounts), 2),
        "outflow_total": round(sum(r.outflow for r in accounts), 2),
        "letters_count": float(len(letters)),
        "letters_amount": round(sum(r.amount or 0.0 for r in letters), 2),
    }


# ---------------------------------------------------------------------------
# Contrôle des chiffres de la prose
# ---------------------------------------------------------------------------

_DIGIT_GROUP = re.compile(r"\d[\d'’  ]*(?:[.,]\d+)?")


def number_tokens(text: str) -> list[str]:
    """Groupes de chiffres d'un texte, normalisés (séparateurs retirés, décimales sur le point)."""
    out = []
    for m in _DIGIT_GROUP.finditer(text):
        s = re.sub(r"[’'  ]", "", m.group(0)).replace(",", ".")
        s = s.rstrip(".")
        if s:
            out.append(s)
    return out


def _spellings(v: Any) -> set[str]:
    """Toutes les écritures acceptables d'une valeur (montant, date, entier, texte)."""
    out: set[str] = set()
    if v is None or isinstance(v, bool):
        return out
    if isinstance(v, (int, float)):
        f = float(v)
        out.add(f"{f:.2f}")
        out.add(f"{f:.1f}")
        if f == int(f):
            out.add(str(int(f)))
        out.add(str(f))
        out.add(str(abs(f)) if f < 0 else str(f))
        out.add(f"{abs(f):.2f}")
        return out
    s = str(v)
    d = as_date(s)
    if d:
        out.update({d.isoformat(), str(d.year), str(d.day), f"{d.day:02d}", str(d.month), f"{d.month:02d}",
                    f"{d.day:02d}.{d.month:02d}.{d.year}", f"{d.day}.{d.month}.{d.year}"})
    out.update(number_tokens(s))
    return out


def allowed_numbers(obj: Any) -> set[str]:
    """Ensemble de toutes les écritures de chiffres présentes dans une structure (tableaux JSON)."""
    allowed: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        else:
            allowed.update(_spellings(x))

    walk(obj)
    return allowed


def _local_grounded(sentence: str, allowed: set[str]) -> bool:
    """Chaque groupe de chiffres de la phrase doit figurer dans les écritures permises."""
    for tok in number_tokens(sentence):
        cands = {tok}
        if "." in tok:
            cands.add(tok.rstrip("0").rstrip("."))
            try:
                cands.add(f"{float(tok):.2f}")
            except ValueError:
                pass
        if not (cands & allowed):
            return False
    return True


def sentence_grounded(sentence: str, facts: Any, allowed: set[str] | None = None) -> bool:
    """Délègue à verify.numbers_grounded s'il existe, sinon au contrôle local."""
    if _verify is not None and hasattr(_verify, "numbers_grounded"):
        try:
            res = _verify.numbers_grounded(sentence, facts)
            if isinstance(res, tuple):
                res = res[0]
            return bool(res)
        except Exception:  # un contrôle qui casse ne doit jamais laisser passer un chiffre
            return False
    return _local_grounded(sentence, allowed if allowed is not None else allowed_numbers(facts))


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[^\d])")


def split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]
    return parts


def filter_prose(prose: str, facts: Any, lang: str) -> tuple[str, int]:
    """Retire les phrases dont un chiffre n'est pas dans les faits ; retourne (texte, nb retirées)."""
    allowed = allowed_numbers(facts)
    kept: list[str] = []
    removed = 0
    for s in split_sentences(prose):
        if sentence_grounded(s, facts, allowed):
            kept.append(s)
        else:
            removed += 1
    text = " ".join(kept)
    if removed:
        text = (text + " " if text else "") + t("report.sentence_removed", lang)
    return text, removed


# ---------------------------------------------------------------------------
# Le paragraphe du modèle
# ---------------------------------------------------------------------------

_LANG_NAME = {"fr": "French", "de": "Swiss Standard German (never use ß, write ss)", "it": "Italian", "en": "English"}


def tables_json(report: Report) -> dict[str, Any]:
    """Les tableaux, compacts, tels que le modèle les reçoit (et tels qu'on contrôle la prose)."""
    return {
        "month": report.month,
        "bills": [{"paper": r.paper_id, "issuer": r.issuer, "invoice_date": r.invoice_date,
                   "due_date": r.due_date, "amount_chf": r.amount, "state": r.state} for r in report.bills],
        "accounts": [{"paper": r.paper_id, "account": r.account, "bank": r.bank, "period_end": r.period_end,
                      "opening": r.opening, "in": r.inflow, "out": r.outflow, "closing": r.closing,
                      "balanced": r.balanced} for r in report.accounts],
        "letters": [{"paper": r.paper_id, "authority": r.authority, "subject": r.subject,
                     "deadline": r.deadline, "amount_chf": r.amount} for r in report.letters],
        "totals": report.totals,
    }


def prose_messages(tables: dict[str, Any], lang: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": (
            "You write one short paragraph (at most 80 words) that sums up a month of Swiss paperwork "
            f"for its owner, in {_LANG_NAME.get(lang, 'French')}. Use only the numbers present in the JSON "
            "tables you receive; never compute, round or invent a figure. Mention what is to pay, "
            "the account movements and the deadlines. Plain prose, no list, no title, no markdown.")},
        {"role": "user", "content": json.dumps(tables, ensure_ascii=False)},
    ]


def write_prose(report: Report, lang: str) -> tuple[str, int]:
    """Demande le paragraphe à Apertus ; retourne (texte brut, jetons). Vide si le modèle est muet."""
    from . import llm  # import tardif : les tests sans modèle n'ont pas besoin du client
    tables = tables_json(report)
    text, usage = llm.chat(prose_messages(tables, lang), json=False, max_tokens=220, temperature=0)
    words = text.strip().split()
    if len(words) > PROSE_MAX_WORDS + 20:  # le modèle a débordé : on coupe à la dernière phrase entière
        cut = " ".join(words[: PROSE_MAX_WORDS + 20])
        sents = split_sentences(cut)
        text = " ".join(sents[:-1]) if len(sents) > 1 else cut
    return text.strip(), int(usage.get("total_tokens", 0))


def build_report(papers: list[Any], month: str, lang: str = "fr", *, with_prose: bool = True) -> Report:
    """Tableaux par le code, totaux par le code, puis (si demandé) le paragraphe contrôlé."""
    lang = i18n.norm_lang(lang)
    bills = bill_rows(papers, month)
    accounts = account_rows(papers, month)
    letters = letter_rows(papers, month)
    report = Report(month=month, lang=lang, bills=bills, accounts=accounts, letters=letters,
                    totals=compute_totals(bills, accounts, letters))
    if not (bills or accounts or letters):
        report.prose = t("report.empty", lang)
        return report
    if not with_prose:
        return report
    try:
        raw, tokens = write_prose(report, lang)
    except Exception as e:  # modèle absent ou en panne : le rapport reste complet, sans prose
        report.prose = t("report.no_prose", lang)
        report.notes.append(f"model: {type(e).__name__}")
        return report
    report.model_tokens = tokens
    if not raw:
        report.prose = t("report.no_prose", lang)
        return report
    report.prose, report.removed_sentences = filter_prose(raw, tables_json(report), lang)
    return report


# ---------------------------------------------------------------------------
# Rendus
# ---------------------------------------------------------------------------

def _state(r: BillRow, lang: str) -> str:
    return t("report.state.to_pay" if r.state == "to_pay" else "report.state.unknown", lang)


def render_markdown(report: Report, lang: str | None = None) -> str:
    lang = i18n.norm_lang(lang or report.lang)
    L = lambda k: t(k, lang)  # noqa: E731
    out = [f"# {L('report.title')} — {i18n.month_label(report.month, lang)}", ""]
    if not (report.bills or report.accounts or report.letters):
        out.append(L("report.empty"))
        return "\n".join(out) + "\n"
    out += [f"## {L('report.bills')}", ""]
    if report.bills:
        out.append(f"| {L('report.col.paper')} | {L('report.col.issuer')} | {L('report.col.invoice_date')} | "
                   f"{L('report.col.due_date')} | {L('report.col.amount')} | {L('report.col.state')} |")
        out.append("|---|---|---|---|---:|---|")
        for r in report.bills:
            out.append(f"| {r.paper_id} | {r.issuer or '—'} | {r.invoice_date or '—'} | {r.due_date or '—'} | "
                       f"{chf(r.amount)} | {_state(r, lang)} |")
        out.append(f"| | **{L('report.total')}** | | | **{chf(report.totals.get('bills_total'))}** | |")
        out.append(f"| | **{L('report.total_to_pay')}** | | | **{chf(report.totals.get('bills_to_pay'))}** | |")
    else:
        out.append(L("report.empty"))
    out += ["", f"## {L('report.accounts')}", ""]
    if report.accounts:
        out.append(f"| {L('report.col.paper')} | {L('report.col.account')} | {L('report.col.opening')} | "
                   f"{L('report.col.in')} | {L('report.col.out')} | {L('report.col.closing')} |")
        out.append("|---|---|---:|---:|---:|---:|")
        for r in report.accounts:
            out.append(f"| {r.paper_id} | {r.account or r.bank or '—'} | {chf(r.opening)} | {chf(r.inflow)} | "
                       f"{chf(r.outflow)} | {chf(r.closing)} |")
            if not r.balanced:
                out.append(f"| | *{L('report.unbalanced')}* ({r.closing_reason}) | | | | |")
    else:
        out.append(L("report.empty"))
    out += ["", f"## {L('report.letters')}", ""]
    if report.letters:
        out.append(f"| {L('report.col.paper')} | {L('report.col.authority')} | {L('report.col.subject')} | "
                   f"{L('report.col.deadline')} | {L('report.col.amount')} |")
        out.append("|---|---|---|---|---:|")
        for r in report.letters:
            out.append(f"| {r.paper_id} | {r.authority or '—'} | {r.subject or '—'} | {r.deadline or '—'} | "
                       f"{chf(r.amount) if r.amount is not None else '—'} |")
    else:
        out.append(L("report.empty"))
    out += ["", f"## {L('report.summary')}", "", report.prose or L("report.no_prose"), ""]
    return "\n".join(out)


def _esc(v: Any) -> str:
    return html.escape("—" if v is None or v == "" else str(v))


def render_html(report: Report, lang: str | None = None) -> str:
    """Fragment HTML (sans <html>), noir sur blanc ; la page lui donne son style."""
    lang = i18n.norm_lang(lang or report.lang)
    L = lambda k: t(k, lang)  # noqa: E731
    link = lambda pid: f'<a class="pid" href="/api/paper/{_esc(pid)}/file" target="_blank">{_esc(pid)}</a>'  # noqa: E731
    h = [f"<h2>{_esc(L('report.title'))} — {_esc(i18n.month_label(report.month, lang))}</h2>"]
    if not (report.bills or report.accounts or report.letters):
        h.append(f"<p class='muted'>{_esc(L('report.empty'))}</p>")
        return "\n".join(h)
    h.append(f"<h3>{_esc(L('report.bills'))}</h3>")
    if report.bills:
        h.append("<table><thead><tr>" + "".join(
            f"<th>{_esc(L(k))}</th>" for k in ("report.col.paper", "report.col.issuer", "report.col.invoice_date",
                                               "report.col.due_date", "report.col.amount", "report.col.state"))
            + "</tr></thead><tbody>")
        for r in report.bills:
            h.append(f"<tr><td>{link(r.paper_id)}</td><td>{_esc(r.issuer)}</td><td class='d'>{_esc(r.invoice_date)}</td>"
                     f"<td class='d'>{_esc(r.due_date)}</td><td class='num'>{_esc(chf(r.amount))}</td><td>{_esc(_state(r, lang))}</td></tr>")
        h.append(f"<tr class='total'><td></td><td>{_esc(L('report.total'))}</td><td></td><td></td>"
                 f"<td class='num'>{_esc(chf(report.totals.get('bills_total')))}</td><td></td></tr>")
        h.append(f"<tr class='total'><td></td><td>{_esc(L('report.total_to_pay'))}</td><td></td><td></td>"
                 f"<td class='num'>{_esc(chf(report.totals.get('bills_to_pay')))}</td><td></td></tr>")
        h.append("</tbody></table>")
    else:
        h.append(f"<p class='muted'>{_esc(L('report.empty'))}</p>")
    h.append(f"<h3>{_esc(L('report.accounts'))}</h3>")
    if report.accounts:
        h.append("<table><thead><tr>" + "".join(
            f"<th>{_esc(L(k))}</th>" for k in ("report.col.paper", "report.col.account", "report.col.opening",
                                               "report.col.in", "report.col.out", "report.col.closing"))
            + "</tr></thead><tbody>")
        for r in report.accounts:
            h.append(f"<tr><td>{link(r.paper_id)}</td><td>{_esc(r.account or r.bank)}</td><td class='num'>{_esc(chf(r.opening))}</td>"
                     f"<td class='num'>{_esc(chf(r.inflow))}</td><td class='num'>{_esc(chf(r.outflow))}</td>"
                     f"<td class='num'>{_esc(chf(r.closing))}</td></tr>")
            if not r.balanced:
                h.append(f"<tr class='warn'><td></td><td colspan='5'>{_esc(L('report.unbalanced'))} "
                         f"<span class='muted'>({_esc(r.closing_reason)})</span></td></tr>")
        h.append("</tbody></table>")
    else:
        h.append(f"<p class='muted'>{_esc(L('report.empty'))}</p>")
    h.append(f"<h3>{_esc(L('report.letters'))}</h3>")
    if report.letters:
        h.append("<table><thead><tr>" + "".join(
            f"<th>{_esc(L(k))}</th>" for k in ("report.col.paper", "report.col.authority", "report.col.subject",
                                               "report.col.deadline", "report.col.amount"))
            + "</tr></thead><tbody>")
        for r in report.letters:
            h.append(f"<tr><td>{link(r.paper_id)}</td><td>{_esc(r.authority)}</td><td>{_esc(r.subject)}</td>"
                     f"<td class='d'>{_esc(r.deadline)}</td><td class='num'>{_esc(chf(r.amount) if r.amount is not None else None)}</td></tr>")
        h.append("</tbody></table>")
    else:
        h.append(f"<p class='muted'>{_esc(L('report.empty'))}</p>")
    h.append(f"<h3>{_esc(L('report.summary'))}</h3>")
    h.append(f"<p class='prose'>{_esc(report.prose or L('report.no_prose'))}</p>")
    return "\n".join(h)
