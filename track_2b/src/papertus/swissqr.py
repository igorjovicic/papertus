"""QR-facture suisse : construire et relire la charge utile du code QR (SPC 0200).

Tout est écrit d'après la spécification publique « Swiss Payment Standards » :

- Charge utile = lignes séparées par ``\\n`` (``\\r\\n`` toléré à la lecture) :
  ``SPC`` / ``0200`` / ``1`` / IBAN / créancier (type S : nom, rue, no, NPA, localité,
  pays) / créancier final (7 lignes vides) / montant / monnaie / débiteur (7 lignes) /
  type de référence ``QRR`` | ``SCOR`` | ``NON`` / référence / message libre /
  ``EPD`` / informations de facturation (facultatif) / procédés alternatifs (0 à 2).
- Référence QRR : 27 chiffres, le dernier est le chiffre de contrôle modulo 10 récursif.
- Référence SCOR (ISO 11649) : ``RF`` + 2 chiffres de contrôle (mod 97) + 21 caractères max.
- IBAN : contrôle mod 97 (ISO 7064) ; QR-IBAN = IID (positions 5 à 9) entre 30000 et 31999.
- ``QRR`` exige un QR-IBAN ; ``SCOR`` et ``NON`` exigent un IBAN ordinaire.

Rien ici ne dépend d'une bibliothèque tierce ; segno dessine le QR dans ``generate.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SPC = "SPC"
VERSION = "0200"
CODING = "1"
TRAILER = "EPD"
REFERENCE_TYPES = ("QRR", "SCOR", "NON")
CURRENCIES = ("CHF", "EUR")


class SwissQRError(ValueError):
    """Charge utile ou valeur de QR-facture invalide."""


# --- IBAN ---------------------------------------------------------------------


def _mod97(numeric: str) -> int:
    """Modulo 97 d'une longue chaîne de chiffres, par tranches (évite les grands entiers)."""
    rest = 0
    for ch in numeric:
        rest = (rest * 10 + int(ch)) % 97
    return rest


def _to_numeric(s: str) -> str:
    """Lettres → nombres (A=10 … Z=35), chiffres inchangés."""
    out: list[str] = []
    for ch in s:
        if ch.isdigit():
            out.append(ch)
        elif "A" <= ch <= "Z":
            out.append(str(ord(ch) - ord("A") + 10))
        else:
            raise SwissQRError(f"invalid character {ch!r}")
    return "".join(out)


def normalize_iban(iban: str) -> str:
    """Enlève espaces et tirets, met en majuscules."""
    return re.sub(r"[\s\-]", "", iban or "").upper()


def iban_valid(iban: str) -> bool:
    """Vrai si l'IBAN a une forme plausible et un contrôle mod 97 correct."""
    s = normalize_iban(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    try:
        return _mod97(_to_numeric(s[4:] + s[:4])) == 1
    except SwissQRError:
        return False


def iban_build(country: str, bban: str) -> str:
    """Calcule les deux chiffres de contrôle et rend l'IBAN compact (ex. ``CH`` + 17 chiffres)."""
    country = country.upper()
    bban = bban.upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise SwissQRError("country must be two letters")
    rest = _mod97(_to_numeric(bban + country + "00"))
    check = 98 - rest
    return f"{country}{check:02d}{bban}"


def iban_format(iban: str) -> str:
    """Groupes de quatre caractères séparés par une espace, comme sur le papier."""
    s = normalize_iban(iban)
    return " ".join(s[i : i + 4] for i in range(0, len(s), 4))


def iban_iid(iban: str) -> int | None:
    """Le numéro de clearing (IID) d'un IBAN suisse ou liechtensteinois, positions 5 à 9."""
    s = normalize_iban(iban)
    if not s.startswith(("CH", "LI")) or len(s) != 21:
        return None
    return int(s[4:9])


def is_qr_iban(iban: str) -> bool:
    """Vrai si l'IID est dans la plage réservée aux QR-IBAN (30000–31999)."""
    iid = iban_iid(iban)
    return iid is not None and 30000 <= iid <= 31999


# --- Référence QRR (modulo 10 récursif) ---------------------------------------

_MOD10_TABLE = (0, 9, 4, 6, 8, 2, 7, 1, 3, 5)


def mod10_recursive(digits: str) -> int:
    """Chiffre de contrôle modulo 10 récursif (table des QR-factures, ex-BVR)."""
    if not digits.isdigit():
        raise SwissQRError("mod10 needs digits only")
    carry = 0
    for ch in digits:
        carry = _MOD10_TABLE[(carry + int(ch)) % 10]
    return (10 - carry) % 10


def qrr_build(body: str) -> str:
    """Complète à 26 chiffres (zéros devant) et ajoute le chiffre de contrôle → 27 chiffres."""
    body = re.sub(r"\s", "", body)
    if not body.isdigit() or len(body) > 26:
        raise SwissQRError("QRR body must be at most 26 digits")
    body = body.zfill(26)
    return body + str(mod10_recursive(body))


def qrr_valid(reference: str) -> bool:
    s = re.sub(r"\s", "", reference or "")
    return len(s) == 27 and s.isdigit() and mod10_recursive(s[:26]) == int(s[26])


def qrr_format(reference: str) -> str:
    """``21 00000 00003 13947 14300 09017`` : un groupe de 2 puis des groupes de 5."""
    s = re.sub(r"\s", "", reference)
    return " ".join([s[:2]] + [s[i : i + 5] for i in range(2, 27, 5)])


# --- Référence SCOR (ISO 11649) -----------------------------------------------


def scor_build(body: str) -> str:
    """``RF`` + contrôle mod 97 + corps alphanumérique (21 caractères au plus)."""
    body = re.sub(r"\s", "", body).upper()
    if not re.fullmatch(r"[A-Z0-9]{1,21}", body):
        raise SwissQRError("SCOR body must be 1–21 alphanumeric characters")
    check = 98 - _mod97(_to_numeric(body + "RF00"))
    return f"RF{check:02d}{body}"


def scor_valid(reference: str) -> bool:
    s = re.sub(r"\s", "", reference or "").upper()
    if not re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", s):
        return False
    return _mod97(_to_numeric(s[4:] + s[:4])) == 1


def scor_format(reference: str) -> str:
    s = re.sub(r"\s", "", reference)
    return " ".join(s[i : i + 4] for i in range(0, len(s), 4))


def reference_valid(reference_type: str, reference: str | None) -> bool:
    """La référence est-elle cohérente avec son type ?"""
    if reference_type == "QRR":
        return qrr_valid(reference or "")
    if reference_type == "SCOR":
        return scor_valid(reference or "")
    if reference_type == "NON":
        return not reference
    return False


# --- Adresses et facture ------------------------------------------------------


@dataclass
class Address:
    """Adresse structurée (type S). ``name`` vide = partie absente (ex. pas de débiteur)."""

    name: str = ""
    street: str = ""
    house_no: str = ""
    postal_code: str = ""
    town: str = ""
    country: str = "CH"

    def is_empty(self) -> bool:
        return not self.name

    def lines(self) -> list[str]:
        """Les 7 lignes de la charge utile (toutes vides si l'adresse est absente)."""
        if self.is_empty():
            return [""] * 7
        return ["S", self.name, self.street, self.house_no, self.postal_code, self.town, self.country]

    def paper_lines(self) -> list[str]:
        """Comme on l'imprime : nom / rue no / NPA localité."""
        street = f"{self.street} {self.house_no}".strip()
        out = [self.name]
        if street:
            out.append(street)
        out.append(f"{self.postal_code} {self.town}".strip())
        return out

    @classmethod
    def from_lines(cls, lines: list[str]) -> "Address":
        """Relit 7 lignes, type S (structuré) ou K (combiné : 2 lignes d'adresse)."""
        if len(lines) != 7:
            raise SwissQRError("address needs 7 lines")
        kind = lines[0]
        if kind == "":
            return cls()
        if kind == "S":
            return cls(
                name=lines[1], street=lines[2], house_no=lines[3],
                postal_code=lines[4], town=lines[5], country=lines[6],
            )
        if kind == "K":
            # Type combiné : ligne 1 = rue et no, ligne 2 = « NPA localité ».
            m = re.match(r"(\d{4,10})\s+(.*)", lines[3] or "")
            return cls(
                name=lines[1], street=lines[2], house_no="",
                postal_code=m.group(1) if m else "", town=m.group(2) if m else lines[3],
                country=lines[6],
            )
        raise SwissQRError(f"unknown address type {kind!r}")


@dataclass
class QRBill:
    """Le contenu d'un code QR de facture suisse."""

    iban: str
    creditor: Address
    amount: float | None = None
    currency: str = "CHF"
    debtor: Address = field(default_factory=Address)
    reference_type: str = "NON"
    reference: str = ""
    message: str = ""
    billing_info: str = ""
    alt_procedures: list[str] = field(default_factory=list)

    # -- validation ------------------------------------------------------------

    def problems(self) -> list[str]:
        """Liste des incohérences (vide = facture valide)."""
        out: list[str] = []
        iban = normalize_iban(self.iban)
        if not iban_valid(iban):
            out.append("iban checksum")
        elif not iban.startswith(("CH", "LI")):
            out.append("iban must be CH or LI")
        if self.reference_type not in REFERENCE_TYPES:
            out.append("reference type")
        elif self.reference_type == "QRR":
            if not is_qr_iban(iban):
                out.append("QRR needs a QR-IBAN")
            if not qrr_valid(self.reference):
                out.append("QRR reference check digit")
        elif self.reference_type == "SCOR":
            if is_qr_iban(iban):
                out.append("SCOR needs a plain IBAN")
            if not scor_valid(self.reference):
                out.append("SCOR reference check digits")
        else:
            if is_qr_iban(iban):
                out.append("NON needs a plain IBAN")
            if self.reference:
                out.append("NON must have no reference")
        if self.currency not in CURRENCIES:
            out.append("currency")
        if self.amount is not None and not (0.01 <= self.amount <= 999_999_999.99):
            out.append("amount range")
        if self.creditor.is_empty():
            out.append("creditor name")
        if len(self.message) > 140:
            out.append("message too long")
        if len(self.billing_info) > 140:
            out.append("billing info too long")
        if len(self.alt_procedures) > 2:
            out.append("too many alternative procedures")
        return out

    def is_valid(self) -> bool:
        return not self.problems()

    # -- charge utile ----------------------------------------------------------

    def payload(self) -> str:
        """La charge utile, lignes séparées par ``\\n``."""
        problems = self.problems()
        if problems:
            raise SwissQRError("; ".join(problems))
        amount = "" if self.amount is None else f"{self.amount:.2f}"
        lines = [SPC, VERSION, CODING, normalize_iban(self.iban)]
        lines += self.creditor.lines()
        lines += [""] * 7                      # créancier final : jamais utilisé
        lines += [amount, self.currency]
        lines += self.debtor.lines()
        lines += [self.reference_type, re.sub(r"\s", "", self.reference), self.message, TRAILER]
        if self.billing_info or self.alt_procedures:
            lines.append(self.billing_info)
        lines += list(self.alt_procedures)
        return "\n".join(lines)

    @classmethod
    def parse(cls, payload: str) -> "QRBill":
        """Relit une charge utile. Lève ``SwissQRError`` si la forme est fausse."""
        text = (payload or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        # Un retour final éventuel ne compte pas.
        while lines and lines[-1] == "" and len(lines) > 31:
            lines.pop()
        if len(lines) < 31:
            raise SwissQRError(f"payload has {len(lines)} lines, at least 31 expected")
        if lines[0] != SPC:
            raise SwissQRError("missing SPC header")
        if lines[1] != VERSION:
            raise SwissQRError(f"unsupported version {lines[1]!r}")
        if lines[2] != CODING:
            raise SwissQRError("unsupported coding")
        iban = normalize_iban(lines[3])
        creditor = Address.from_lines(lines[4:11])
        # lines[11:18] = créancier final (ignoré)
        amount_s = lines[18].strip()
        amount = float(amount_s) if amount_s else None
        currency = lines[19].strip()
        debtor = Address.from_lines(lines[20:27])
        reference_type = lines[27].strip()
        reference = re.sub(r"\s", "", lines[28])
        message = lines[29]
        if lines[30] != TRAILER:
            raise SwissQRError("missing EPD trailer")
        billing_info = lines[31] if len(lines) > 31 else ""
        alt = [ln for ln in lines[32:34] if ln]
        bill = cls(
            iban=iban, creditor=creditor, amount=amount, currency=currency, debtor=debtor,
            reference_type=reference_type, reference=reference, message=message,
            billing_info=billing_info, alt_procedures=alt,
        )
        return bill

    # -- affichage papier ------------------------------------------------------

    def reference_for_paper(self) -> str:
        if self.reference_type == "QRR":
            return qrr_format(self.reference)
        if self.reference_type == "SCOR":
            return scor_format(self.reference)
        return ""


def parse_payload(payload: str) -> QRBill:
    """Raccourci : ``QRBill.parse``."""
    return QRBill.parse(payload)


def looks_like_payload(text: str) -> bool:
    """Vrai si ce texte commence comme une charge utile de QR-facture."""
    return (text or "").lstrip().startswith(SPC + "\n") or (text or "").lstrip().startswith(SPC + "\r\n")
