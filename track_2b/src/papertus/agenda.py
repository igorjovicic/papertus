"""L'agenda : chaque événement que portent les papiers, calculé par le code, jamais par le modèle.

Un événement = une date vérifiée (ou dite par le modèle, jamais un blanc) sur un papier :
l'échéance d'une facture (FACTURE), le délai d'une lettre officielle (DÉLAI), la fin de période
d'un relevé (RELEVÉ). Le code regroupe (en retard / cette semaine / ce mois / plus tard / fait),
additionne, écrit la ligne « Aujourd'hui » et le fichier .ics que le calendrier de la personne
lit : le rappel souverain, sans serveur ni service de notification.
`today` est toujours passé explicitement (captures d'écran et tests reproductibles).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import i18n
from .i18n import t
from .report import as_date, as_float, chf, field_triplet, field_value, paper_attr

WEEK_DAYS = 7
GROUPS = ("overdue", "week", "month", "later", "done")
KIND_LABEL = {"bill": "agenda.kind.bill", "deadline": "agenda.kind.deadline", "statement": "agenda.kind.statement"}


@dataclass
class Event:
    id: str                      # « p094:bill » — stable, sert à l'état « fait » et au .ics
    date: str                    # ISO
    kind: str                    # bill | deadline | statement
    paper_id: str
    who: str | None              # émetteur, autorité ou banque
    what: str | None             # objet, sujet ou compte
    amount: float | None         # montant dû ; solde de clôture pour un relevé
    amount_status: str = "blank"   # statut du champ montant (verified | model | blank)
    amount_reason: str | None = None
    action: str | None = None    # action_required d'une lettre
    date_status: str = "model"   # verified | model
    done: bool = False
    group: str = "later"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event(paper: Any, kind: str, date_key: str, who_key: str, what_key: str, amount_key: str) -> Event | None:
    value, status, _ = field_triplet(paper, date_key)
    d = as_date(value)
    if d is None or status == "blank":
        return None  # une date blanche ne fait pas d'événement
    amount, a_status, a_reason = field_triplet(paper, amount_key)
    pid = paper_attr(paper, "id", "")
    return Event(
        id=f"{pid}:{kind}", date=d.isoformat(), kind=kind, paper_id=pid,
        who=_s(field_value(paper, who_key)), what=_s(field_value(paper, what_key)),
        amount=as_float(amount), amount_status=a_status if as_float(amount) is not None else "blank",
        amount_reason=a_reason if as_float(amount) is None else None,
        action=_s(field_value(paper, "action_required")) if kind == "deadline" else None,
        date_status="verified" if status == "verified" else "model",
    )


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def events_from_papers(papers: Iterable[Any], done: set[str] | None = None) -> list[Event]:
    """Tous les événements des papiers, datés, triés par date puis par id."""
    done = done or set()
    out: list[Event] = []
    for p in papers:
        kind = paper_attr(p, "kind")
        if kind == "qr_bill":
            e = _event(p, "bill", "due_date", "issuer", "purpose", "amount_chf")
        elif kind == "official_letter":
            e = _event(p, "deadline", "deadline", "authority", "subject", "amount_chf")
        elif kind == "bank_statement":
            e = _event(p, "statement", "period_end", "bank", "account_iban", "closing_balance")
        else:
            e = None
        if e is not None:
            e.done = e.id in done
            out.append(e)
    out.sort(key=lambda e: (e.date, e.paper_id, e.kind))
    return out


def group_of(e: Event, today: date) -> str:
    d = as_date(e.date) or today
    if e.done:
        return "done"
    if d < today:
        # un relevé passé dont le solde est vérifié est de l'histoire, pas un retard ;
        # un relevé passé dont le solde est blanc demande un regard
        return "done" if (e.kind == "statement" and e.amount is not None) else "overdue"
    if d <= today + timedelta(days=WEEK_DAYS - 1):
        return "week"
    if (d.year, d.month) == (today.year, today.month):
        return "month"
    return "later"


def group_events(events: list[Event], today: date) -> dict[str, list[Event]]:
    groups: dict[str, list[Event]] = {g: [] for g in GROUPS}
    for e in events:
        e.group = group_of(e, today)
        groups[e.group].append(e)
    return groups


def latest_balances(papers: Iterable[Any]) -> tuple[float, int, list[str]]:
    """(somme des derniers soldes vérifiés par compte, nombre de comptes dont le dernier relevé
    est blanc, ids de ces relevés). Un compte = un IBAN (sinon la banque + le titulaire)."""
    latest: dict[str, Any] = {}
    for p in papers:
        if paper_attr(p, "kind") != "bank_statement":
            continue
        end = as_date(field_value(p, "period_end"))
        if end is None:
            continue
        key = _s(field_value(p, "account_iban")) or f"{field_value(p, 'bank')}|{field_value(p, 'holder')}"
        if key not in latest or end > latest[key][0]:
            latest[key] = (end, p)
    total, blank, blank_ids = 0.0, 0, []
    for _, p in latest.values():
        closing, status, _ = field_triplet(p, "closing_balance")
        v = as_float(closing)
        if v is None or status != "verified":
            blank += 1
            blank_ids.append(paper_attr(p, "id"))
        else:
            total += v
    return round(total, 2), blank, sorted(blank_ids)


def figures(events: list[Event], papers: Iterable[Any], today: date) -> dict[str, Any]:
    """Les quatre grands chiffres du bandeau, tous calculés par le code."""
    week_end = today + timedelta(days=WEEK_DAYS - 1)
    open_ev = [e for e in events if not e.done]
    due7 = [e for e in open_ev if e.kind == "bill" and today <= (as_date(e.date) or today) <= week_end]
    overdue = [e for e in open_ev if (as_date(e.date) or today) < today and e.kind in ("bill", "deadline")]
    deadlines_month = [e for e in open_ev if e.kind == "deadline"
                       and ((as_date(e.date) or today).year, (as_date(e.date) or today).month) == (today.year, today.month)]
    balance, blank, blank_ids = latest_balances(papers)
    return {
        "due_7_total": round(sum(e.amount or 0.0 for e in due7), 2), "due_7_count": len(due7),
        "overdue_total": round(sum(e.amount or 0.0 for e in overdue), 2), "overdue_count": len(overdue),
        "deadlines_month": len(deadlines_month),
        "balance_total": balance, "balance_blank": blank, "balance_blank_ids": blank_ids,
    }


def _plural(key: str, n: int, lang: str, **kw) -> str:
    return t(f"{key}_1" if n == 1 else f"{key}_n", lang, n=n, **kw)


def today_line(events: list[Event], today: date, lang: str) -> str:
    """« 3 factures à payer cette semaine (CHF 9'843.12), 2 délais, 1 relevé non vérifié. »"""
    lang = i18n.norm_lang(lang)
    week_end = today + timedelta(days=WEEK_DAYS - 1)
    open_ev = [e for e in events if not e.done]
    in_week = lambda e: today <= (as_date(e.date) or today) <= week_end  # noqa: E731
    bills = [e for e in open_ev if e.kind == "bill" and in_week(e)]
    deadlines = [e for e in open_ev if e.kind == "deadline" and in_week(e)]
    unverified = [e for e in open_ev if e.kind == "statement" and e.amount is None]
    overdue = [e for e in open_ev if (as_date(e.date) or today) < today and e.kind in ("bill", "deadline")]
    parts: list[str] = []
    if overdue:
        parts.append(_plural("agenda.today.overdue", len(overdue), lang))
    if bills:
        parts.append(_plural("agenda.today.bills", len(bills), lang, total=chf(round(sum(e.amount or 0.0 for e in bills), 2))))
    if deadlines:
        parts.append(_plural("agenda.today.deadlines", len(deadlines), lang))
    if unverified:
        parts.append(_plural("agenda.today.statements", len(unverified), lang))
    if not parts:
        return t("agenda.today.none", lang)
    return ", ".join(parts) + "."


def build_agenda(papers: list[Any], today: date, done: set[str] | None = None, lang: str = "fr") -> dict[str, Any]:
    """La structure complète pour la page et l'API : chiffres, ligne du jour, groupes d'événements."""
    lang = i18n.norm_lang(lang)
    events = events_from_papers(papers, done)
    groups = group_events(events, today)
    return {
        "today": today.isoformat(),
        "lang": lang,
        "line": today_line(events, today, lang),
        "figures": figures(events, papers, today),
        "groups": {g: [e.to_dict() for e in evs] for g, evs in groups.items()},
        "counts": {g: len(evs) for g, evs in groups.items()},
    }


# ---------------------------------------------------------------------------
# Le fichier .ics : un VEVENT par événement ouvert, une alarme deux jours avant
# ---------------------------------------------------------------------------

def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Repli des lignes longues (RFC 5545 : 75 octets, continuation par un espace)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not out else 74):
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def event_summary(e: Event, lang: str) -> str:
    amount = f" · CHF {chf(e.amount)}" if e.amount is not None else ""
    who = e.who or e.paper_id
    if e.kind == "bill":
        return t("agenda.ics.bill", lang, who=who, amount=amount)
    if e.kind == "deadline":
        return t("agenda.ics.deadline", lang, who=who, amount=amount)
    return t("agenda.ics.statement", lang, who=who, amount=amount if e.amount is not None else f" · {t('agenda.unverified', lang)}")


def to_ics(events: list[Event], lang: str = "fr", *, now: datetime | None = None, include_done: bool = False) -> str:
    """Texte iCalendar (CRLF) ; un VEVENT par événement ouvert, DTSTART = la date, VALARM −2 jours."""
    lang = i18n.norm_lang(lang)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{i18n.APP_NAME}//Agenda//{lang.upper()}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{i18n.APP_NAME}"]
    for e in events:
        if e.done and not include_done:
            continue
        d = as_date(e.date)
        if d is None:
            continue
        desc = f"{i18n.APP_NAME} [{e.paper_id}] · {t(KIND_LABEL[e.kind], lang)}"
        if e.what:
            desc += f" · {e.what}"
        if e.action and t(f"action.{e.action}", lang) not in ("", f"action.{e.action}"):
            desc += f" · {t(f'action.{e.action}', lang)}"
        elif e.action and e.action != "none":
            desc += f" · {e.action}"
        if e.kind == "statement" and e.amount is None and e.amount_reason:
            desc += f" · {t('agenda.unverified', lang)}: {e.amount_reason}"
        summary = event_summary(e, lang)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.id}@{i18n.APP_NAME.lower()}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-P2D", f"DESCRIPTION:{_ics_escape(summary)}", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
