"""Les vérifications : du code pur, jamais le modèle.

Principe : le modèle lit et propose, le code contrôle. Chaque valeur extraite
reçoit un statut : « verified » (contrôlée par le code : QR, somme, checksum,
présence dans le texte), « model » (le modèle seul, aucune preuve) ou « blank »
(refusée, avec la raison). Un chiffre que le code ne peut pas confirmer là où il
devrait l'être n'est pas gardé : mieux vaut un blanc qu'une invention.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from . import swissqr as _swissqr
from .schema import Facts, Field

try:  # les listes de clés par sorte de papier viennent du schéma partagé
    from .schema import TRUTH_KEYS
except ImportError:  # pragma: no cover - sécurité si le schéma évolue
    TRUTH_KEYS = {
        "qr_bill": ["issuer", "recipient", "amount_chf", "currency", "iban", "reference",
                    "reference_type", "invoice_number", "invoice_date", "due_date", "purpose"],
        "bank_statement": ["bank", "account_iban", "holder", "period_start", "period_end",
                           "opening_balance", "closing_balance", "movements", "currency"],
        "official_letter": ["authority", "recipient", "letter_date", "subject", "letter_kind",
                            "amount_chf", "deadline", "reference_number", "action_required"],
    }

YEAR_MIN, YEAR_MAX = 2024, 2027
_BALANCE_ROW = re.compile(
    r"anfangssaldo|schlusssaldo|saldovortrag|solde initial|solde final|solde au|solde d'ouverture|solde de cl[ôo]ture|"
    r"saldo iniziale|saldo finale|opening balance|closing balance|^saldo\b|^solde\b|^total\b|totale\b",
    re.IGNORECASE,
)
# « Belastung : Fr. 3182.70   Gutschrift : Fr. 0.— » et ses formes fr / it, sur une seule ligne
# (espaces horizontaux seulement : l'en-tête de colonne « Gutschrift » suivi d'une date ne compte pas).
_HS = r"[^\S\n]*"
_NUM = r"([\d'’\u00a0\u202f]+(?:[ ]\d{3})*(?:[.,]\d{2}|\.(?:—|–|-{1,2}))?)"
_DEBIT_TOTAL = re.compile(r"(?:belastung(?:en)?|d[ée]bits?|addebit[oi])" + _HS + ":?" + _HS + r"(?:CHF|Fr\.?)?" + _HS + _NUM, re.IGNORECASE)
_CREDIT_TOTAL = re.compile(r"(?:gutschrift(?:en)?|cr[ée]dits?|accredit[oi])" + _HS + ":?" + _HS + r"(?:CHF|Fr\.?)?" + _HS + _NUM, re.IGNORECASE)
LETTER_KINDS = {"tax_decision", "payment_summons", "premium_notice", "commune_notice", "permit_decision"}
AMOUNT_KEYS = {"amount_chf", "opening_balance", "closing_balance"}
DATE_KEYS = {"invoice_date", "due_date", "letter_date", "deadline", "period_start", "period_end"}
ID_KEYS = {"invoice_number", "reference_number"}

MONTHS = {
    # fr
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    # de
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    # it
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "agosto": 8, "settembre": 9, "ottobre": 10, "dicembre": 12,
}

# ---------------------------------------------------------------------------
# Nombres à la suisse
# ---------------------------------------------------------------------------

_SEP = "['’’   ]"
# 1'234.50 · 1 234.50 · 1234.— · 1234.- · 1234.50 · 1234,50 · 1234
_AMOUNT_RE = re.compile(
    r"(?<![\d.,])"
    r"(\d{1,3}(?:" + _SEP + r"\d{3})+|\d+)"
    r"(?:[.,](\d{2})(?!\d)|\.(?:—|–|-{1,2})(?!\d))?"
    r"(?!\d)"
)


def normalise_number_text(text: str) -> str:
    """Rend un texte comparable : 1'234.— → 1234.00, 1 234.50 → 1234.50."""
    def one(m: re.Match[str]) -> str:
        whole = re.sub(_SEP, "", m.group(1))
        dec = m.group(2) or "00"
        return f"{whole}.{dec}"
    return _AMOUNT_RE.sub(one, text)


def parse_amount(value: Any) -> float | None:
    """'CHF 1'234.50' → 1234.5 ; 1234 → 1234.0 ; None/vide/illisible → None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    neg = s.startswith("-") or s.startswith("−") or s.endswith("-")
    m = _AMOUNT_RE.search(s)
    if not m:
        return None
    whole = re.sub(_SEP, "", m.group(1))
    dec = m.group(2) or "00"
    v = float(f"{whole}.{dec}")
    return -v if neg else v


def amounts_in_text(text: str) -> set[float]:
    """Tous les montants que le texte contient, dans tous les formats suisses."""
    found: set[float] = set()
    for m in _AMOUNT_RE.finditer(text):
        whole = re.sub(_SEP, "", m.group(1))
        dec = m.group(2) or "00"
        found.add(round(float(f"{whole}.{dec}"), 2))
    return found


def printed_totals(text: str) -> tuple[float, float] | None:
    """La ligne des totaux d'un relevé : (total des débits, total des crédits), ou None."""
    ds, cs = _DEBIT_TOTAL.findall(text), _CREDIT_TOTAL.findall(text)
    if not ds or not cs:
        return None
    d, c = parse_amount(ds[-1].strip()), parse_amount(cs[-1].strip())  # la ligne des totaux est en bas
    if d is None or c is None:
        return None
    return abs(d), abs(c)


def amount_in_text(value: float | None, text: str, amounts: set[float] | None = None) -> bool:
    if value is None:
        return False
    amounts = amounts if amounts is not None else amounts_in_text(text)
    v = round(abs(value), 2)
    return any(abs(v - a) < 0.005 for a in amounts)


# ---------------------------------------------------------------------------
# Checksums : IBAN (mod 97), QRR (mod 10 récursif), SCOR (RF mod 97)
# ---------------------------------------------------------------------------

def clean_iban(value: Any) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", "", str(value)).upper()
    return s or None


_IBAN_IN_TEXT = re.compile(r"\b(CH|LI)\s?\d{2}(?:\s?[A-Z0-9]{4}){4}\s?[A-Z0-9]\b")


def ibans_in_text(text: str) -> list[str]:
    """Les IBAN suisses imprimés dans le texte dont le contrôle mod 97 passe (sans espaces)."""
    out: list[str] = []
    for m in _IBAN_IN_TEXT.finditer(text):
        s = re.sub(r"\s+", "", m.group(0)).upper()
        if iban_valid(s) and s not in out:
            out.append(s)
    return out


_QRR_IN_TEXT = re.compile(r"(?<!\d)\d{2}(?:\s?\d{5}){5}(?!\d)|(?<!\d)\d{27}(?!\d)")
_SCOR_IN_TEXT = re.compile(r"\bRF\d{2}(?:\s?[A-Z0-9]{1,5}){1,6}\b")


def references_in_text(text: str) -> tuple[list[str], list[str]]:
    """Les références imprimées dont le chiffre de contrôle passe : (QRR 27 chiffres, SCOR RF…)."""
    qrr = []
    for m in _QRR_IN_TEXT.finditer(text):
        c = re.sub(r"\s+", "", m.group(0))
        if qrr_valid(c) and c not in qrr:
            qrr.append(c)
    scor = []
    for m in _SCOR_IN_TEXT.finditer(text):
        c = re.sub(r"\s+", "", m.group(0)).upper()
        if scor_valid(c) and c not in scor:
            scor.append(c)
    return qrr, scor


def iban_field_from_model_or_text(raw: Any, text: str) -> Field:
    """L'IBAN du modèle s'il passe le contrôle ; sinon celui imprimé dans le texte qui le prolonge
    (le modèle oublie souvent le dernier chiffre isolé : CH37 0873 … 6676 2)."""
    iban = clean_iban(raw)
    if iban and iban_valid(iban):
        return Field(iban, "verified", source="iban")
    printed = ibans_in_text(text)
    if iban:
        for cand in printed:
            if cand.startswith(iban) or iban.startswith(cand):
                return Field(cand, "verified", source="iban", reason=f"model said {iban}, completed from text")
        return Field(None, "blank", reason=f"iban checksum failed for {iban}")
    if len(printed) == 1:
        return Field(printed[0], "verified", source="iban", reason="not given by model, read from text")
    return Field(None, "blank", reason="not given")


def iban_valid(iban: str | None) -> bool:
    """Contrôle mod 97 (délégué au module partagé swissqr)."""
    s = clean_iban(iban)
    return bool(s) and _swissqr.iban_valid(s)


def is_qr_iban(iban: str | None) -> bool:
    """Un QR-IBAN suisse porte un clearing 30000–31999 en positions 5–9."""
    s = clean_iban(iban)
    return bool(s) and _swissqr.is_qr_iban(s)


def qrr_valid(ref: str | None) -> bool:
    """Référence QR : 27 chiffres, le dernier = modulo 10 récursif."""
    if ref is None:
        return False
    s = re.sub(r"\s+", "", str(ref))
    return bool(re.fullmatch(r"\d{27}", s)) and _swissqr.qrr_valid(s)


def scor_valid(ref: str | None) -> bool:
    """Référence créancier ISO 11649 : RF + 2 chiffres de contrôle + 1–21 caractères."""
    if ref is None:
        return False
    s = re.sub(r"\s+", "", str(ref)).upper()
    return bool(re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", s)) and _swissqr.scor_valid(s)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def date_in_range(d: date | None) -> bool:
    return d is not None and YEAR_MIN <= d.year <= YEAR_MAX


def dates_in_text(text: str) -> set[date]:
    """Toutes les dates lisibles du texte : 30.09.2026, 2026-09-30, 30 septembre 2026, 30. September 2026."""
    found: set[date] = set()
    for m in re.finditer(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", text):
        d, mo, y = map(int, m.groups())
        try:
            found.add(date(y, mo, d))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        y, mo, d = map(int, m.groups())
        try:
            found.add(date(y, mo, d))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})(?:er|\.)?\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", text):
        mo = MONTHS.get(m.group(2).lower())
        if mo:
            try:
                found.add(date(int(m.group(3)), mo, int(m.group(1))))
            except ValueError:
                pass
    return found


# ---------------------------------------------------------------------------
# QR : lecture de la charge utile SPC
# ---------------------------------------------------------------------------

def qr_fields(payload: str | None) -> dict[str, Any]:
    """Charge utile « SPC » → {iban, creditor_name, amount, currency, reference_type, reference, message}.

    On s'appuie sur swissqr.parse_payload si le module partagé l'expose ; sinon on lit
    la disposition normalisée (ligne par ligne, SPC / 0200 / 1 / IBAN / adresse créancier…).
    """
    if not payload or not payload.startswith("SPC"):
        return {}
    try:
        got = _adapt_qr(_swissqr.parse_payload(payload))
        if got:
            return got
    except Exception:
        pass  # charge utile hors norme : on tente la lecture ligne à ligne ci-dessous
    lines = payload.replace("\r\n", "\n").split("\n")
    if len(lines) < 20:
        return {}
    # Disposition SPC 0200 : 0 SPC, 1 version, 2 codage, 3 IBAN, 4-10 créancier (AdrTp, Nom, rue, no, NPA, lieu, pays),
    # 11-17 créancier final (vide), 18 montant, 19 monnaie, 20-26 débiteur, 27 type réf, 28 réf, 29 message, 30 EPD
    def at(i: int) -> str:
        return lines[i].strip() if i < len(lines) else ""
    return {
        "iban": clean_iban(at(3)),
        "creditor_name": at(5) or None,
        "amount": parse_amount(at(18)) if at(18) else None,
        "currency": at(19) or None,
        "debtor_name": at(21) or None,
        "reference_type": at(27) or None,
        "reference": re.sub(r"\s+", "", at(28)) or None,
        "message": at(29) or None,
    }


def _adapt_qr(parsed: Any) -> dict[str, Any]:
    """Accepte un dict ou un objet du module swissqr et en tire nos clés."""
    if parsed is None:
        return {}
    get = (lambda k: parsed.get(k)) if isinstance(parsed, dict) else (lambda k: getattr(parsed, k, None))
    aliases = {
        "iban": ("iban", "account"),
        "creditor_name": ("creditor_name", "creditor", "name"),
        "amount": ("amount", "amount_chf"),
        "currency": ("currency",),
        "debtor_name": ("debtor_name", "debtor"),
        "reference_type": ("reference_type", "ref_type"),
        "reference": ("reference", "ref"),
        "message": ("message", "unstructured_message", "purpose"),
    }
    out: dict[str, Any] = {}
    for key, names in aliases.items():
        for n in names:
            v = get(n)
            if v is None:
                continue
            if key in ("creditor_name", "debtor_name") and not isinstance(v, str):
                v = getattr(v, "name", None) if not isinstance(v, dict) else v.get("name")
            if v is not None:
                out[key] = v
                break
    if out.get("amount") is not None:
        out["amount"] = parse_amount(out["amount"])
    if out.get("iban"):
        out["iban"] = clean_iban(out["iban"])
    if out.get("reference"):
        out["reference"] = re.sub(r"\s+", "", str(out["reference"]))
    return out if out.get("iban") else {}


# ---------------------------------------------------------------------------
# La vérification d'un papier
# ---------------------------------------------------------------------------

def _str(value: Any) -> str | None:
    if value is None or isinstance(value, (list, dict)):
        return None
    s = str(value).strip()
    return s or None


def _same_text(a: Any, b: Any) -> bool:
    return re.sub(r"\s+", " ", str(a or "")).strip().lower() == re.sub(r"\s+", " ", str(b or "")).strip().lower()


def _date_field(raw: Any, text_dates: set[date]) -> Field:
    if raw is None or str(raw).strip() == "":
        return Field(None, "blank", reason="not given")
    d = parse_iso_date(raw)
    if d is None:
        return Field(None, "blank", reason=f"date: unreadable {str(raw)[:20]!r}")
    if not date_in_range(d):
        return Field(None, "blank", reason=f"date: {d.isoformat()} outside {YEAR_MIN}-{YEAR_MAX}")
    if d in text_dates:
        return Field(d.isoformat(), "verified", source="text")
    return Field(d.isoformat(), "model", reason="date not found in text")


def _amount_field(raw: Any, amounts: set[float], *, must_be_in_text: bool) -> Field:
    if raw is None or str(raw).strip() == "":
        return Field(None, "blank", reason="not given")
    v = parse_amount(raw)
    if v is None:
        return Field(None, "blank", reason=f"amount: unreadable {str(raw)[:20]!r}")
    if any(abs(round(abs(v), 2) - a) < 0.005 for a in amounts):
        return Field(round(v, 2), "verified", source="text")
    if must_be_in_text:
        return Field(None, "blank", reason=f"amount {v:.2f} not found in text")
    return Field(round(v, 2), "model", reason="amount not found in text")


def _text_field(raw: Any) -> Field:
    s = _str(raw)
    return Field(s, "model") if s else Field(None, "blank", reason="not given")


def verify(kind: str, raw: dict[str, Any], text: str, qr_payload: str | None) -> Facts:
    """Valeurs brutes du modèle + texte + QR → Facts dont chaque champ porte son statut."""
    raw = dict(raw or {})
    amounts = amounts_in_text(text)
    text_dates = dates_in_text(text)
    fields: dict[str, Field] = {}

    if kind == "qr_bill":
        fields = _verify_qr_bill(raw, text, amounts, text_dates, qr_payload)
    elif kind == "bank_statement":
        fields = _verify_bank_statement(raw, text, amounts, text_dates)
    else:
        fields = _verify_official_letter(raw, text, amounts, text_dates)

    for key in TRUTH_KEYS.get(kind, []):
        fields.setdefault(key, Field(None, "blank", reason="not given"))
    return Facts(fields=fields)


def _verify_qr_bill(raw: dict[str, Any], text: str, amounts: set[float], text_dates: set[date],
                    qr_payload: str | None) -> dict[str, Field]:
    f: dict[str, Field] = {}
    qr = qr_fields(qr_payload)

    # Les champs que le QR tranche : montant, IBAN, référence, type, monnaie, créancier.
    if qr:
        model_iban = clean_iban(raw.get("iban"))
        f["iban"] = Field(qr["iban"], "verified", source="qr",
                          reason=None if model_iban in (None, qr["iban"]) else f"model said {model_iban}")
        if qr.get("amount") is not None:
            mv = parse_amount(raw.get("amount_chf"))
            f["amount_chf"] = Field(round(qr["amount"], 2), "verified", source="qr",
                                    reason=None if mv is None or abs(mv - qr["amount"]) < 0.005 else f"model said {mv:.2f}")
        else:
            f["amount_chf"] = _amount_field(raw.get("amount_chf"), amounts, must_be_in_text=False)
        if qr.get("currency"):
            f["currency"] = Field(qr["currency"], "verified", source="qr")
        rt = (qr.get("reference_type") or "NON").upper()
        f["reference_type"] = Field(rt, "verified", source="qr",
                                    reason=None if _same_text(raw.get("reference_type"), rt) or raw.get("reference_type") in (None, "") else f"model said {raw.get('reference_type')}")
        if rt == "NON" or not qr.get("reference"):
            f["reference"] = Field(None, "verified", source="qr", reason="no reference on the QR")
        else:
            mr = re.sub(r"\s+", "", str(raw.get("reference") or ""))
            f["reference"] = Field(qr["reference"], "verified", source="qr",
                                   reason=None if mr in ("", qr["reference"]) else f"model said {mr}")
        if qr.get("creditor_name"):
            f["issuer"] = Field(qr["creditor_name"], "verified", source="qr",
                                reason=None if _same_text(raw.get("issuer"), qr["creditor_name"]) or not raw.get("issuer") else f"model said {raw.get('issuer')}")
        if qr.get("debtor_name") and not raw.get("recipient"):
            f["recipient"] = Field(qr["debtor_name"], "verified", source="qr")
    else:
        # Facture sans QR : seul le texte et les checksums peuvent confirmer.
        f["amount_chf"] = _amount_field(raw.get("amount_chf"), amounts, must_be_in_text=False)
        f["iban"] = iban_field_from_model_or_text(raw.get("iban"), text)
        # Le type de référence est décidé par le code, jamais par le modèle : une référence QRR
        # ou SCOR imprimée dont le chiffre de contrôle passe, sinon NON.
        rt = _str(raw.get("reference_type"))
        rt = rt.upper() if rt else None
        model_ref = re.sub(r"\s+", "", str(raw.get("reference") or "")) or None
        qrr_printed, scor_printed = references_in_text(text)
        if qrr_printed:
            f["reference"] = Field(qrr_printed[0], "verified", source="text",
                                   reason=None if model_ref in (None, qrr_printed[0]) else f"model said {model_ref}")
            f["reference_type"] = Field("QRR", "verified", source="text", reason=None if rt in (None, "QRR") else f"model said {rt}")
        elif scor_printed:
            f["reference"] = Field(scor_printed[0], "verified", source="text",
                                   reason=None if model_ref in (None, scor_printed[0].upper()) else f"model said {model_ref}")
            f["reference_type"] = Field("SCOR", "verified", source="text", reason=None if rt in (None, "SCOR") else f"model said {rt}")
        else:
            f["reference_type"] = Field("NON", "verified", source="rule", reason="no QR and no valid reference printed"
                                        + ("" if rt in (None, "NON") else f"; model said {rt}"))
            f["reference"] = Field(None, "blank", reason="no reference on a NON bill" + (f" (model said {model_ref})" if model_ref else ""))
        cur = _str(raw.get("currency"))
        f["currency"] = Field(cur.upper(), "model") if cur else Field(None, "blank", reason="not given")

    f.setdefault("issuer", _text_field(raw.get("issuer")))
    f.setdefault("recipient", _text_field(raw.get("recipient")))
    f["purpose"] = _text_field(raw.get("purpose"))
    f["invoice_number"] = _id_field(raw.get("invoice_number"), text)
    f["invoice_date"] = _date_field(raw.get("invoice_date"), text_dates)
    f["due_date"] = _date_field(raw.get("due_date"), text_dates)
    inv, due = parse_iso_date(f["invoice_date"].value), parse_iso_date(f["due_date"].value)
    if inv and due and due < inv:
        f["due_date"] = Field(None, "blank", reason=f"date: due {due.isoformat()} before invoice {inv.isoformat()}")
    return f


def _id_field(raw: Any, text: str) -> Field:
    """Un identifiant (no de facture, référence d'une lettre) : vérifié s'il est dans le texte tel quel."""
    s = _str(raw)
    if not s:
        return Field(None, "blank", reason="not given")
    compact = re.sub(r"\s+", "", s).lower()
    if compact and compact in re.sub(r"\s+", "", text).lower():
        return Field(s, "verified", source="text")
    return Field(s, "model", reason="id not found in text")


def unique_sign_assignment(amounts: list[float], debit_total: float) -> list[bool] | None:
    """Quel sous-ensemble des montants (en valeur absolue) fait exactement le total des débits ?

    Retourne [est_débit…] si UNE seule répartition existe, None sinon (aucune ou plusieurs :
    le code refuse de choisir). Énumération avec élagage ; n ≤ 22 pour rester instantané.
    """
    n = len(amounts)
    if n == 0 or n > 22:
        return None
    cents = [int(round(abs(a) * 100)) for a in amounts]
    target = int(round(debit_total * 100))
    order = sorted(range(n), key=lambda i: -cents[i])
    suffix = [0] * (n + 1)
    for k in range(n - 1, -1, -1):
        suffix[k] = suffix[k + 1] + cents[order[k]]
    solutions: list[list[bool]] = []
    chosen = [False] * n

    def walk(k: int, remaining: int) -> None:
        if len(solutions) > 1:
            return
        if remaining == 0:
            solutions.append(list(chosen))
            if k < n and suffix[k] == 0:
                solutions.append(list(chosen))  # des montants nuls rendraient la solution ambiguë
            return
        if k == n or remaining < 0 or suffix[k] < remaining:
            return
        i = order[k]
        chosen[i] = True
        walk(k + 1, remaining - cents[i])
        chosen[i] = False
        walk(k + 1, remaining)

    walk(0, target)
    return solutions[0] if len(solutions) == 1 else None


_LINE_DATE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})[./](\d{4})\b")


def align_movement_dates(movements: list[dict[str, Any]], text: str) -> int:
    """Relit la date de chaque mouvement sur le papier : la ligne de date la plus proche
    au-dessus du libellé (ou du montant). Corrige en place ; retourne le nombre de corrections."""
    lines = text.splitlines()
    low = [ln.strip().lower() for ln in lines]
    fixed = 0
    for mv in movements:
        label = re.sub(r"\s+", " ", str(mv.get("label") or "")).strip().lower()
        if len(label) < 4:
            continue
        hits = [i for i, ln in enumerate(low) if label in ln]
        if len(hits) != 1:
            continue
        for j in range(hits[0], max(-1, hits[0] - 4), -1):
            m = _LINE_DATE.match(lines[j])
            if m:
                d, mo, y = map(int, m.groups())
                try:
                    printed = date(y, mo, d).isoformat()
                except ValueError:
                    break
                if mv.get("date") != printed:
                    mv["date"] = printed
                    fixed += 1
                break
    return fixed


def _verify_bank_statement(raw: dict[str, Any], text: str, amounts: set[float], text_dates: set[date]) -> dict[str, Field]:
    f: dict[str, Field] = {}
    f["bank"] = _text_field(raw.get("bank"))
    f["holder"] = _text_field(raw.get("holder"))
    cur = _str(raw.get("currency"))
    f["currency"] = Field(cur.upper(), "model") if cur else Field(None, "blank", reason="not given")
    f["account_iban"] = iban_field_from_model_or_text(raw.get("account_iban"), text)
    f["period_start"] = _date_field(raw.get("period_start"), text_dates)
    f["period_end"] = _date_field(raw.get("period_end"), text_dates)
    ps, pe = parse_iso_date(f["period_start"].value), parse_iso_date(f["period_end"].value)
    if ps and pe and pe < ps:
        f["period_end"] = Field(None, "blank", reason=f"date: period end {pe.isoformat()} before start {ps.isoformat()}")

    f["opening_balance"] = _amount_field(raw.get("opening_balance"), amounts, must_be_in_text=False)
    closing = _amount_field(raw.get("closing_balance"), amounts, must_be_in_text=False)

    # Les mouvements : chacun doit porter un montant lisible dans le texte.
    kept: list[dict[str, Any]] = []
    dropped = 0
    movements = raw.get("movements")
    if isinstance(movements, list):
        for mv in movements:
            if not isinstance(mv, dict):
                dropped += 1
                continue
            if _BALANCE_ROW.search(str(mv.get("label") or "")):
                continue  # une ligne de solde n'est pas un mouvement : le code l'écarte
            amt = parse_amount(mv.get("amount"))
            if amt is None or not amount_in_text(amt, text, amounts):
                dropped += 1
                continue
            d = parse_iso_date(mv.get("date"))
            kept.append({"date": d.isoformat() if d else None, "label": _str(mv.get("label")), "amount": round(amt, 2)})
    totals = printed_totals(text)
    repaired: str | None = None
    dated = align_movement_dates(kept, text)
    if kept and totals:
        # Σ débits et Σ crédits des mouvements contre la ligne des totaux imprimée.
        debits = round(-sum(m["amount"] for m in kept if m["amount"] < 0), 2)
        credits = round(sum(m["amount"] for m in kept if m["amount"] > 0), 2)
        if (abs(debits - totals[0]) >= 0.01 or abs(credits - totals[1]) >= 0.01) \
                and abs(round(sum(abs(m["amount"]) for m in kept), 2) - round(totals[0] + totals[1], 2)) < 0.01:
            # Les montants sont tous là mais des signes manquent (la colonne débit / crédit se perd à
            # l'extraction). S'il n'existe qu'UNE répartition des signes qui fait les deux totaux, le
            # code l'applique ; sinon il refuse.
            signs = unique_sign_assignment([m["amount"] for m in kept], totals[0])
            if signs is not None:
                changed = 0
                for m, is_debit in zip(kept, signs):
                    want = -abs(m["amount"]) if is_debit else abs(m["amount"])
                    if m["amount"] != want:
                        m["amount"] = want
                        changed += 1
                repaired = f"sign of {changed} movement(s) set from printed totals"
                debits = round(-sum(m["amount"] for m in kept if m["amount"] < 0), 2)
                credits = round(sum(m["amount"] for m in kept if m["amount"] > 0), 2)
        if abs(debits - totals[0]) >= 0.01 or abs(credits - totals[1]) >= 0.01:
            f["movements"] = Field(None, "blank", source="arithmetic",
                                   reason=f"movement_totals: debits {debits:.2f} / credits {credits:.2f}, "
                                          f"statement prints {totals[0]:.2f} / {totals[1]:.2f}")
        else:
            notes = [n for n in (repaired, f"date of {dated} movement(s) read from the paper" if dated else None,
                                 f"{dropped} movement(s) dropped: amount not in text" if dropped else None) if n]
            f["movements"] = Field(kept, "verified", source="arithmetic", reason="; ".join(notes) or None)
    elif kept:
        f["movements"] = Field(kept, "verified", source="text",
                               reason=None if not dropped else f"{dropped} movement(s) dropped: amount not in text")
    else:
        f["movements"] = Field(None, "blank", reason="no movement with an amount found in text" if dropped else "not given")

    # L'arithmétique : ouverture + Σ mouvements = clôture, à un centime près.
    opening, closing_v = f["opening_balance"].value, closing.value
    balanced: bool | None = None
    if opening is not None and closing_v is not None and isinstance(movements, list) and movements:
        total = round(float(opening) + sum(m["amount"] for m in kept), 2)
        balanced = abs(total - float(closing_v)) < 0.01 and dropped == 0
        if balanced:
            f["closing_balance"] = Field(round(float(closing_v), 2), "verified", source="arithmetic")
            f["opening_balance"] = Field(round(float(opening), 2), "verified", source="arithmetic")
        else:
            f["closing_balance"] = Field(None, "blank", reason="arithmetic",
                                         source="arithmetic")
            f["closing_balance"].reason = f"arithmetic: opening {float(opening):.2f} + movements = {total:.2f}, statement says {float(closing_v):.2f}"
    else:
        f["closing_balance"] = closing if closing_v is not None else Field(None, "blank", reason=closing.reason or "not given")
    f["balanced"] = Field(balanced, "verified" if balanced is not None else "blank", source="arithmetic",
                          reason=None if balanced is not None else "cannot check: missing opening, closing or movements")
    return f


def _verify_official_letter(raw: dict[str, Any], text: str, amounts: set[float], text_dates: set[date]) -> dict[str, Field]:
    f: dict[str, Field] = {}
    f["authority"] = _text_field(raw.get("authority"))
    f["recipient"] = _text_field(raw.get("recipient"))
    f["subject"] = _text_field(raw.get("subject"))
    f["action_required"] = _text_field(raw.get("action_required"))
    lk = _str(raw.get("letter_kind"))
    lk = lk.lower().strip() if lk else None
    f["letter_kind"] = Field(lk, "model") if lk in LETTER_KINDS else Field(None, "blank", reason=f"unknown letter_kind {lk!r}")
    f["letter_date"] = _date_field(raw.get("letter_date"), text_dates)
    f["deadline"] = _date_field(raw.get("deadline"), text_dates)
    ld, dl = parse_iso_date(f["letter_date"].value), parse_iso_date(f["deadline"].value)
    if ld and dl and dl < ld:
        f["deadline"] = Field(None, "blank", reason=f"date: deadline {dl.isoformat()} before letter {ld.isoformat()}")
    # Une lettre : le montant est dans le texte ou il n'y en a pas.
    f["amount_chf"] = _amount_field(raw.get("amount_chf"), amounts, must_be_in_text=True)
    f["reference_number"] = _id_field(raw.get("reference_number"), text)
    return f


# ---------------------------------------------------------------------------
# Ancrage des chiffres d'une prose (rapport, réponses)
# ---------------------------------------------------------------------------

def _collect_values(obj: Any, numbers: set[float], dates: set[str], digit_strings: set[str]) -> None:
    """Parcourt facts (Facts, Field, dict, liste, scalaires) et récolte les valeurs numériques."""
    if obj is None or isinstance(obj, bool):
        return
    if isinstance(obj, Facts):
        _collect_values(obj.fields, numbers, dates, digit_strings)
    elif isinstance(obj, Field):
        _collect_values(obj.value, numbers, dates, digit_strings)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_values(v, numbers, dates, digit_strings)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect_values(v, numbers, dates, digit_strings)
    elif isinstance(obj, (int, float)):
        numbers.add(round(abs(float(obj)), 2))
    elif isinstance(obj, date):
        _add_date(obj, dates, digit_strings)
    elif isinstance(obj, str):
        d = parse_iso_date(obj)
        if d:
            _add_date(d, dates, digit_strings)
            return
        v = parse_amount(obj) if _AMOUNT_RE.fullmatch(obj.strip().replace("CHF", "").strip()) else None
        if v is not None:
            numbers.add(round(abs(v), 2))
        for g in re.findall(r"\d+", obj):
            digit_strings.add(g)
        compact = re.sub(r"\D", "", obj)
        if compact:
            digit_strings.add(compact)


def _add_date(d: date, dates: set[str], digit_strings: set[str]) -> None:
    dates.add(d.isoformat())
    digit_strings.update({str(d.year), f"{d.month:02d}", f"{d.day:02d}", str(d.month), str(d.day)})


_DATE_IN_PROSE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})\b")
_NUM_IN_PROSE = re.compile(r"\d+(?:[.,]\d+)?")
_CITATION = re.compile(r"\[p\d{3,4}\]")
_GROUP_SEP = re.compile(r"(?<=\d)['’\u2019\u00a0\u202f ](?=\d{3}\b)")
_DASH_CENTS = re.compile(r"(?<=\d)\.(?:—|–|-{1,2})(?!\d)")


def _normalise_prose(prose: str) -> str:
    """Retire les citations [pNNN], les séparateurs de milliers et écrit .— en .00."""
    t = _CITATION.sub(" ", prose)
    t = _GROUP_SEP.sub("", t)
    return _DASH_CENTS.sub(".00", t)


def numbers_grounded(prose: str, facts: Any) -> tuple[bool, list[str]]:
    """Chaque groupe de chiffres (≥ 2 chiffres) de la prose doit exister dans les faits.

    facts : tout conteneur de valeurs (Facts, dict, liste de dicts, scalaires). Les
    montants sont comparés à 0.01 près, les dates exactement, les identifiants par
    leurs chiffres. Retourne (ok, [groupes manquants, tels qu'écrits]).
    """
    numbers: set[float] = set()
    dates: set[str] = set()
    digit_strings: set[str] = set()
    _collect_values(facts, numbers, dates, digit_strings)

    missing: list[str] = []
    text = _normalise_prose(prose)

    def check_date(m: re.Match[str]) -> str:
        d = parse_iso_date(m.group(1))
        if d is None or d.isoformat() not in dates:
            missing.append(m.group(1))
        return " "
    text = _DATE_IN_PROSE.sub(check_date, text)

    for m in _NUM_IN_PROSE.finditer(text):
        tok = m.group(0)
        if len(re.sub(r"\D", "", tok)) < 2:
            continue
        try:
            v = round(float(tok.replace(",", ".")), 2)
        except ValueError:
            missing.append(tok)
            continue
        if any(abs(v - n) < 0.005 for n in numbers):
            continue
        if "." not in tok and "," not in tok:
            if tok in digit_strings:
                continue
            # Un identifiant cité par son début ou sa fin (« compte se terminant par 3911 ») : au moins
            # 4 chiffres, préfixe ou suffixe d'un groupe de chiffres des faits (IBAN, référence).
            if len(tok) >= 4 and any(len(d) > len(tok) and (d.endswith(tok) or d.startswith(tok)) for d in digit_strings):
                continue
        missing.append(tok)
    return (not missing, missing)
