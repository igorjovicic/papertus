"""La ligne de base : Apertus seul, sur le texte brut, sans QR, sans contrôle.

Ce que le modèle dit est pris tel quel. C'est la ligne « Baseline » du rapport ;
elle montre ce qu'on perd quand le code ne vérifie rien.
"""

from __future__ import annotations

from typing import Any

import pymupdf

from . import llm
from .read import KEY_HINTS
from .schema import Facts, Paper, TRUTH_KEYS

MAX_TEXT_CHARS = 14000
MAX_CONTEXT_CHARS = 28000  # tous les papiers dans un seul prompt, borné (≈ 14k jetons : ~2 caractères par jeton sur ces textes)


def raw_text(path: str) -> tuple[str, int]:
    """Le texte du PDF, rien d'autre (pas de rendu d'image, pas de QR)."""
    with pymupdf.open(path) as doc:
        return "\n\n".join(p.get_text("text").strip("\n") for p in doc), doc.page_count


def _hint_all_kinds() -> str:
    parts = []
    for kind, keys in KEY_HINTS.items():
        parts.append(f'if kind is "{kind}": ' + "{" + ", ".join(f'"{k}": {v}' for k, v in keys.items()) + "}")
    return '{"kind": "qr_bill" | "bank_statement" | "official_letter", "facts": {...}} where facts is ' + "; ".join(parts)


def baseline_extract(text: str) -> tuple[str, dict[str, Any]]:
    """Un seul appel : le genre et les faits, valeurs prises telles quelles."""
    prompt = (
        "Read this Swiss paper. Say its kind and fill the facts for that kind as JSON. "
        "Numbers as plain numbers, dates as YYYY-MM-DD.\n\nPAPER TEXT:\n" + text[:MAX_TEXT_CHARS]
    )
    ans = llm.json_answer([{"role": "user", "content": prompt}], _hint_all_kinds(), max_tokens=1800)
    kind = str(ans.get("kind", "")).strip()
    facts = ans.get("facts")
    if kind not in TRUTH_KEYS:
        kind = "official_letter"
    if not isinstance(facts, dict):
        # Certains modèles mettent les clés à plat : on les prend là où elles sont.
        facts = {k: ans.get(k) for k in TRUTH_KEYS[kind]}
    return kind, {k: facts.get(k) for k in TRUTH_KEYS[kind]}


def baseline_paper(path: str, *, paper_id: str | None = None, rel_file: str | None = None,
                   text: str | None = None, pages: int | None = None) -> Paper:
    """PDF → Paper « Baseline » : tous les champs au statut model, aucune vérification."""
    import os

    if text is None or pages is None:
        text, pages = raw_text(path)
    try:
        kind, raw = baseline_extract(text)
        facts = Facts.from_plain(kind, raw, status="model")
    except (llm.ModelError, llm.ModelAnswerError) as e:
        kind, facts = "official_letter", Facts.empty("official_letter", reason=f"model: {str(e)[:80]}")
    return Paper(
        id=paper_id or os.path.splitext(os.path.basename(path))[0],
        file=rel_file or path,
        kind=kind,
        language="fr",
        pages=pages,
        text=text,
        qr_payload=None,
        facts=facts,
    )


def baseline_questions(all_texts: dict[str, str] | list[str], question: str) -> str:
    """Tous les textes dans un prompt, aucune citation exigée, aucune vérification."""
    items = all_texts.items() if isinstance(all_texts, dict) else enumerate(all_texts)
    chunks: list[str] = []
    used = 0
    for key, txt in items:
        block = f"--- paper {key} ---\n{txt.strip()}\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            chunks.append("--- (more papers omitted, context full) ---\n")
            break
        chunks.append(block)
        used += len(block)
    messages = [
        {"role": "system", "content": "You answer questions about the user's papers. Answer in the language "
                                      "of the question, in one short sentence with the figure or date asked."},
        {"role": "user", "content": "PAPERS:\n" + "".join(chunks) + "\nQUESTION: " + question},
    ]
    text, _ = llm.chat(messages, json=False, max_tokens=120)
    return text.strip()
