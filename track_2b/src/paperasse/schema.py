"""Schéma partagé de Paperasse : un papier, ses faits, chaque fait avec sa provenance.

Ce module ne dépend que de la bibliothèque standard. Il est lu par tous les autres
modules (lecture, vérification, rapport, questions, interface). Les règles :

- Un ``Field`` est une valeur extraite avec son statut : ``verified`` (vérifiée par du
  code), ``model`` (seulement dite par le modèle), ``blank`` (refusée, avec une raison).
- Les montants sont des ``float`` en CHF, les dates des chaînes ISO ``YYYY-MM-DD``.
- Les ``movements`` d'un relevé bancaire sont une liste de ``{date, label, amount}``
  avec ``amount`` signé (négatif = débit).
- ``TRUTH_KEYS`` dit exactement quelles clés de faits existent pour chaque genre de
  papier ; ``NUMERIC_KEYS`` et ``DATE_KEYS`` disent lesquelles sont des nombres ou des dates.

Le JSON de vérité (``data/truth/p001.json``) a la forme
``{"id", "file", "kind", "language", "pages", "traps", "facts": {clé: valeur nue}}``
plus, pour un relevé bancaire, ``"balanced": true|false`` (``false`` = les mouvements ne
font pas le solde final imprimé ; le solde final attendu de notre lecture est alors un
blanc avec la raison ``arithmetic``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# --- Types fermés -------------------------------------------------------------

Kind = Literal["qr_bill", "bank_statement", "official_letter"]
Lang = Literal["fr", "de", "it"]
Status = Literal["verified", "model", "blank"]
Source = Literal["qr", "text", "arithmetic", "iban", "date"]
LetterKind = Literal[
    "tax_decision", "payment_summons", "premium_notice", "commune_notice", "permit_decision"
]
ReferenceType = Literal["QRR", "SCOR", "NON"]

KINDS: tuple[str, ...] = ("qr_bill", "bank_statement", "official_letter")
LANGS: tuple[str, ...] = ("fr", "de", "it")
STATUSES: tuple[str, ...] = ("verified", "model", "blank")
SOURCES: tuple[str, ...] = ("qr", "text", "arithmetic", "iban", "date")
LETTER_KINDS: tuple[str, ...] = (
    "tax_decision", "payment_summons", "premium_notice", "commune_notice", "permit_decision"
)
REFERENCE_TYPES: tuple[str, ...] = ("QRR", "SCOR", "NON")

# Valeurs canoniques de ``action_required`` pour une lettre officielle.
ACTION_VALUES: tuple[str, ...] = ("pay", "reply", "submit_documents", "none")

# --- Quelles clés pour quel genre ---------------------------------------------

TRUTH_KEYS: dict[str, list[str]] = {
    "qr_bill": [
        "issuer", "recipient", "amount_chf", "currency", "iban", "reference",
        "reference_type", "invoice_number", "invoice_date", "due_date", "purpose",
    ],
    "bank_statement": [
        "bank", "account_iban", "holder", "period_start", "period_end",
        "opening_balance", "closing_balance", "movements", "currency",
    ],
    "official_letter": [
        "authority", "recipient", "letter_date", "subject", "letter_kind",
        "amount_chf", "deadline", "reference_number", "action_required",
    ],
}

# Clés dont la valeur est un nombre (float, CHF). Les montants des mouvements sont
# imbriqués dans ``movements`` et se vérifient à part (arithmétique du relevé).
NUMERIC_KEYS: frozenset[str] = frozenset({"amount_chf", "opening_balance", "closing_balance"})

# Clés dont la valeur est une date ISO ``YYYY-MM-DD``.
DATE_KEYS: frozenset[str] = frozenset(
    {"invoice_date", "due_date", "period_start", "period_end", "letter_date", "deadline"}
)

# Clés qui sont une liste (une seule : les mouvements du relevé).
LIST_KEYS: frozenset[str] = frozenset({"movements"})

# Noms des pièges que le générateur peut planter (voir ``generate.py``).
TRAP_NAMES: tuple[str, ...] = (
    "second_amount",        # un acompte déjà versé ou un HT à côté du TTC
    "footer_date",          # une date d'en-tête ou de pied de page qui n'est pas celle du papier
    "creditor_name_drift",  # le nom du créancier dans le texte diffère un peu du QR (le QR fait foi)
    "unbalanced",           # relevé dont les mouvements ne font pas le solde final imprimé
    "no_amount",            # lettre officielle sans aucun montant
    "no_qr",                # facture sans code QR (type de référence NON)
    "format_apostrophe",    # nombres écrits 1'234.50
    "format_space",         # nombres écrits CHF 1 234.50
    "format_fr_dash",       # nombres écrits Fr. 1234.—
)


# --- Dataclasses --------------------------------------------------------------


@dataclass
class Field:
    """Une valeur extraite et sa provenance."""

    value: Any = None            # str | float | list | None ; None = blanc
    status: str = "blank"        # "verified" | "model" | "blank"
    reason: str | None = None    # pourquoi c'est blanc / quel contrôle a échoué (anglais court)
    source: str | None = None    # où ça a été vérifié : "qr", "text", "arithmetic", "iban", "date"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Field":
        return cls(
            value=d.get("value"),
            status=d.get("status", "blank"),
            reason=d.get("reason"),
            source=d.get("source"),
        )

    @classmethod
    def blank(cls, reason: str, source: str | None = None) -> "Field":
        return cls(value=None, status="blank", reason=reason, source=source)

    @classmethod
    def verified(cls, value: Any, source: str) -> "Field":
        return cls(value=value, status="verified", reason=None, source=source)

    @classmethod
    def model(cls, value: Any) -> "Field":
        return cls(value=value, status="model", reason=None, source=None)


@dataclass
class Check:
    """Le résultat d'un contrôle de code (nom, réussi ou non, détail court)."""

    name: str                    # "qr_amount", "iban_checksum", "statement_arithmetic", ...
    ok: bool
    detail: str = ""             # texte court en anglais (niveau code)
    key: str | None = None       # clé de fait concernée, si une seule

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Check":
        return cls(name=d["name"], ok=bool(d["ok"]), detail=d.get("detail", ""), key=d.get("key"))


@dataclass
class Facts:
    """Les faits d'un papier : une entrée ``Field`` par clé de ``TRUTH_KEYS[kind]``."""

    fields: dict[str, Field] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: f.to_dict() for k, f in self.fields.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Facts":
        return cls(fields={k: Field.from_dict(v) for k, v in (d or {}).items()})

    @classmethod
    def empty(cls, kind: str, reason: str = "not extracted") -> "Facts":
        """Toutes les clés du genre, en blanc."""
        return cls(fields={k: Field.blank(reason) for k in TRUTH_KEYS[kind]})

    @classmethod
    def from_plain(cls, kind: str, plain: dict[str, Any], status: str = "model") -> "Facts":
        """Construit des faits depuis des valeurs nues (vérité ou réponse brute du modèle).
        Les clés absentes du genre sont ignorées ; les clés manquantes deviennent des blancs."""
        fields: dict[str, Field] = {}
        for k in TRUTH_KEYS[kind]:
            if k in plain and plain[k] is not None:
                fields[k] = Field(value=plain[k], status=status)
            else:
                fields[k] = Field.blank("missing")
        return cls(fields=fields)

    def plain(self) -> dict[str, Any]:
        """Valeurs nues (``None`` pour un blanc), même forme que le JSON de vérité."""
        return {k: f.value for k, f in self.fields.items()}

    def get(self, key: str) -> Any:
        f = self.fields.get(key)
        return None if f is None else f.value


@dataclass
class Paper:
    """Un papier lu : fichier, genre, langue, texte, QR, faits."""

    id: str                        # p001 …
    file: str                      # chemin relatif, ex. data/papers/p001.pdf
    kind: str                      # Kind
    language: str                  # Lang
    pages: int = 1
    text: str = ""                 # texte extrait (notre lecture le remplit)
    qr_payload: str | None = None
    facts: Facts = field(default_factory=Facts)
    traps: list[str] = field(default_factory=list)   # générateur seulement (JSON de vérité)
    checks: list[Check] = field(default_factory=list)  # contrôles de code effectués

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "kind": self.kind,
            "language": self.language,
            "pages": self.pages,
            "text": self.text,
            "qr_payload": self.qr_payload,
            "facts": self.facts.to_dict(),
            "traps": list(self.traps),
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Paper":
        return cls(
            id=d["id"],
            file=d.get("file", ""),
            kind=d["kind"],
            language=d.get("language", "fr"),
            pages=int(d.get("pages", 1)),
            text=d.get("text", ""),
            qr_payload=d.get("qr_payload"),
            facts=Facts.from_dict(d.get("facts", {})),
            traps=list(d.get("traps", [])),
            checks=[Check.from_dict(c) for c in d.get("checks", [])],
        )

    @classmethod
    def from_json(cls, s: str) -> "Paper":
        return cls.from_dict(json.loads(s))


# --- JSON de vérité -----------------------------------------------------------


@dataclass
class Truth:
    """Le JSON de vérité d'un papier généré (valeurs nues, pas de ``Field``)."""

    id: str
    file: str
    kind: str
    language: str
    facts: dict[str, Any]
    traps: list[str] = field(default_factory=list)
    pages: int = 1
    balanced: bool | None = None   # relevés bancaires seulement

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "file": self.file,
            "kind": self.kind,
            "language": self.language,
            "pages": self.pages,
            "traps": list(self.traps),
            "facts": dict(self.facts),
        }
        if self.kind == "bank_statement":
            d["balanced"] = True if self.balanced is None else bool(self.balanced)
        return d

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Truth":
        return cls(
            id=d["id"],
            file=d.get("file", ""),
            kind=d["kind"],
            language=d.get("language", "fr"),
            facts=dict(d.get("facts", {})),
            traps=list(d.get("traps", [])),
            pages=int(d.get("pages", 1)),
            balanced=d.get("balanced"),
        )

    @classmethod
    def from_json(cls, s: str) -> "Truth":
        return cls.from_dict(json.loads(s))

    def expected_blank(self, key: str) -> bool:
        """Vrai si notre lecture DOIT laisser cette clé en blanc (la vérité est un piège)."""
        if self.kind == "bank_statement" and key == "closing_balance" and self.balanced is False:
            return True
        return self.facts.get(key) is None


# --- Petites aides partagées --------------------------------------------------


def load_truth(path: str) -> Truth:
    with open(path, encoding="utf-8") as fh:
        return Truth.from_json(fh.read())


def load_paper(path: str) -> Paper:
    with open(path, encoding="utf-8") as fh:
        return Paper.from_json(fh.read())


def is_numeric_key(key: str) -> bool:
    return key in NUMERIC_KEYS


def is_date_key(key: str) -> bool:
    return key in DATE_KEYS


def scored_keys(kind: str) -> list[str]:
    """Les clés comptées par l'évaluation : nombres, dates et identifiants
    (IBAN, référence, numéro de facture, numéro de référence)."""
    ids = {"iban", "reference", "reference_type", "invoice_number", "account_iban", "reference_number"}
    return [k for k in TRUTH_KEYS[kind] if k in NUMERIC_KEYS or k in DATE_KEYS or k in ids]
