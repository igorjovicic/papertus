"""Lire un papier : texte + QR par du code, genre par des règles, faits par Apertus,
puis vérification par du code. Le modèle propose, le code contrôle.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pymupdf  # texte des PDF et rendu des pages en image
import zxingcpp  # lecture des codes QR dans l'image rendue
from PIL import Image

from . import llm, verify
from .schema import Check, Facts, Paper, TRUTH_KEYS

RENDER_DPI = 200
MAX_TEXT_CHARS = 14000  # ce qu'on montre au modèle, au plus (≈ 4–5 pages denses)

# ---------------------------------------------------------------------------
# 1. Texte et QR
# ---------------------------------------------------------------------------


def decode_qr(image: Image.Image) -> str | None:
    """Le premier code QR dont le contenu commence par « SPC » (QR-facture suisse)."""
    try:
        results = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
    except Exception:
        return None
    for r in results:
        if r.text and r.text.lstrip().startswith("SPC"):
            return r.text
    return None


def read_pdf(path: str) -> tuple[str, str | None, int]:
    """(texte de toutes les pages, charge utile QR ou None, nombre de pages)."""
    texts: list[str] = []
    payload: str | None = None
    with pymupdf.open(path) as doc:
        pages = doc.page_count
        for page in doc:
            texts.append(page.get_text("text"))
            if payload is None:
                pix = page.get_pixmap(dpi=RENDER_DPI, colorspace=pymupdf.csRGB, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                payload = decode_qr(img)
    text = "\n\n".join(t.strip("\n") for t in texts)
    return text, payload, pages


# ---------------------------------------------------------------------------
# 2. Le genre du papier : des règles d'abord, le modèle seulement si c'est ambigu
# ---------------------------------------------------------------------------

_BANK_WORDS = re.compile(
    r"relev[ée] de compte|kontoauszug|estratto conto|\bsolde\b|\bsaldo\b|solde initial|solde final|"
    r"anfangssaldo|schlusssaldo|saldo iniziale|saldo finale",
    re.IGNORECASE,
)
_INVOICE_WORDS = re.compile(r"\bfacture\b|\brechnung\b|\bfattura\b|\binvoice\b", re.IGNORECASE)
_LETTER_WORDS = re.compile(
    r"d[ée]cision|verf[üu]gung|decisione|commune|gemeinde|comune|\boffice\b|\bamt\b|\bufficio\b|"
    r"sommation|mahnung|betreibung|poursuite|esecuzione|diffida|prime|pr[äa]mie|premio|"
    r"taxation|steuer|imposta|imp[ôo]t|permis|bewilligung|permesso|autorisation|canton|kanton|cantone",
    re.IGNORECASE,
)
_LANG_WORDS = {
    "fr": re.compile(r"\b(le|la|les|des|pour|votre|facture|montant|date|compte|solde|et|du|de)\b", re.IGNORECASE),
    "de": re.compile(r"\b(der|die|das|und|für|ihre|rechnung|betrag|datum|konto|saldo|mit|von|zahlbar)\b", re.IGNORECASE),
    "it": re.compile(r"\b(il|la|gli|per|vostra|fattura|importo|data|conto|saldo|e|di|del|della)\b", re.IGNORECASE),
}


def guess_language(text: str) -> str:
    scores = {lang: len(rx.findall(text)) for lang, rx in _LANG_WORDS.items()}
    # « la », « e », « di » sont ambigus fr/it : on tranche sur les mots longs quand c'est serré.
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "fr"


def classify(text: str, qr_payload: str | None) -> str:
    """Genre par des règles ; le modèle ne tranche que si les règles hésitent."""
    if qr_payload and qr_payload.lstrip().startswith("SPC"):
        return "qr_bill"
    bank = len(_BANK_WORDS.findall(text))
    invoice = len(_INVOICE_WORDS.findall(text))
    letter = len(_LETTER_WORDS.findall(text))
    if bank >= 2:
        return "bank_statement"
    if invoice and not letter:
        return "qr_bill"
    if letter and not invoice:
        return "official_letter"
    if not os.getenv("LLM_BASE_URL"):
        return "official_letter" if letter >= invoice else "qr_bill"
    try:
        ans = llm.json_answer(
            [{"role": "user", "content": "Classify this Swiss paper. Kinds: qr_bill (an invoice / bill), "
              "bank_statement (account statement with balances and movements), official_letter "
              "(letter from an authority, insurer or commune).\n\nPAPER TEXT:\n" + text[:MAX_TEXT_CHARS]}],
            '{"kind": "qr_bill" | "bank_statement" | "official_letter"}',
            max_tokens=40,
        )
        kind = str(ans.get("kind", "")).strip()
        if kind in TRUTH_KEYS:
            return kind
    except (llm.ModelError, llm.ModelAnswerError):
        pass
    return "official_letter" if letter >= invoice else "qr_bill"


# ---------------------------------------------------------------------------
# 3. Extraction par Apertus : les clés du genre, null quand on ne sait pas
# ---------------------------------------------------------------------------

KEY_HINTS: dict[str, dict[str, str]] = {
    "qr_bill": {
        "issuer": "string, the company or person who issued the bill (the creditor)",
        "recipient": "string, who must pay",
        "amount_chf": "number, the total amount still to pay, in CHF (not a deposit, not a net-before-tax amount)",
        "currency": "string, CHF or EUR",
        "iban": "string, the creditor's IBAN, no spaces",
        "reference": "string, the payment reference (27 digits QRR or RF… SCOR), no spaces",
        "reference_type": "string, QRR | SCOR | NON",
        "invoice_number": "string, the invoice number as printed",
        "invoice_date": "string YYYY-MM-DD, the date the invoice was issued (not a footer or letterhead date)",
        "due_date": "string YYYY-MM-DD, the payment deadline",
        "purpose": "string, what the bill is for, short",
    },
    "bank_statement": {
        "bank": "string, the bank's name",
        "account_iban": "string, the account IBAN, no spaces",
        "holder": "string, the account holder",
        "period_start": "string YYYY-MM-DD",
        "period_end": "string YYYY-MM-DD",
        "opening_balance": "number, the balance at the start of the period",
        "closing_balance": "number, the balance at the end of the period as printed",
        "movements": "array of {date: YYYY-MM-DD, label: string, amount: number signed, negative = debit}, one per booking line; NOT the opening or closing balance lines",
        "currency": "string, CHF or EUR",
    },
    "official_letter": {
        "authority": "string, who sent the letter (office, commune, insurer)",
        "recipient": "string, who received it",
        "letter_date": "string YYYY-MM-DD, the date of the letter",
        "subject": "string, the subject line, short",
        "letter_kind": "string, one of tax_decision | payment_summons | premium_notice | commune_notice | permit_decision",
        "amount_chf": "number, the amount claimed in CHF, or null if the letter states no amount",
        "deadline": "string YYYY-MM-DD, the deadline given to act, or null",
        "reference_number": "string, the file / reference number of the letter",
        "action_required": "string, one of pay | reply | submit_documents | none",
    },
}


def _schema_hint(kind: str) -> str:
    return "{" + ", ".join(f'"{k}": {v}' for k, v in KEY_HINTS[kind].items()) + "}"


def extract(paper_kind: str, text: str, qr_payload: str | None) -> dict[str, Any]:
    """Demande à Apertus les clés du genre. Inconnu = null, jamais une supposition."""
    kind = paper_kind if paper_kind in TRUTH_KEYS else "official_letter"
    prompt = (
        f"Read this Swiss paper (kind: {kind}) and fill the JSON keys from the text only. "
        "If a value is not printed on the paper, use null. Never guess, never compute, never round. "
        "Copy numbers exactly as printed, converted to plain numbers (1'234.50 -> 1234.50). "
        "Dates as YYYY-MM-DD.\n\nPAPER TEXT:\n" + text[:MAX_TEXT_CHARS]
    )
    max_tokens = 1800 if kind == "bank_statement" else 700
    raw = llm.json_answer([{"role": "user", "content": prompt}], _schema_hint(kind), max_tokens=max_tokens)
    return {k: raw.get(k) for k in TRUTH_KEYS[kind]}


# ---------------------------------------------------------------------------
# 4. Tout enchaîner
# ---------------------------------------------------------------------------


def checks_from_facts(facts: Facts) -> list[Check]:
    """Les contrôles de code, lisibles, dérivés des statuts des champs."""
    out: list[Check] = []
    for key, f in facts.fields.items():
        if f.status == "verified":
            out.append(Check(name=f"{key}:{f.source or 'code'}", ok=True, detail=f.reason or "", key=key))
        elif f.status == "blank" and f.reason and f.reason != "not given":
            out.append(Check(name=f"{key}:{f.source or 'code'}", ok=False, detail=f.reason, key=key))
    return out


def build_paper(*, paper_id: str, file: str, kind: str, text: str, qr: str | None, pages: int,
                raw: dict[str, Any] | None, error: str | None = None) -> Paper:
    """Assemble un Paper à partir des morceaux (texte, QR, réponse brute du modèle) : la
    vérification est du code pur, on peut donc la rejouer sans rappeler le modèle."""
    if raw is None:
        facts = Facts.empty(kind, reason=f"model: {(error or 'no answer')[:80]}")
    else:
        facts = verify.verify(kind, raw, text, qr)
    return Paper(
        id=paper_id,
        file=file,
        kind=kind,
        language=guess_language(text),
        pages=pages,
        text=text,
        qr_payload=qr,
        facts=facts,
        checks=checks_from_facts(facts),
    )


def read_raw(path: str) -> dict[str, Any]:
    """Lecture → genre → extraction brute (la seule étape qui parle au modèle)."""
    return read_raw_from(*read_pdf(path))


def read_raw_from(text: str, qr: str | None, pages: int) -> dict[str, Any]:
    """Comme read_raw, à partir d'un texte et d'un QR déjà lus."""
    kind = classify(text, qr)
    try:
        raw: dict[str, Any] | None = extract(kind, text, qr)
        error = None
    except (llm.ModelError, llm.ModelAnswerError) as e:
        raw, error = None, str(e)
    return {"text": text, "qr": qr, "pages": pages, "kind": kind, "raw": raw, "error": error}


def read_paper(path: str, *, paper_id: str | None = None, rel_file: str | None = None) -> Paper:
    """Fichier PDF → Paper avec ses faits vérifiés (lecture → genre → extraction → vérification)."""
    got = read_raw(path)
    pid = paper_id or os.path.splitext(os.path.basename(path))[0]
    return build_paper(paper_id=pid, file=rel_file or path, **got)
