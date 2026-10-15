"""Les pastilles d'exemple : chaque question proposée dans chaque langue doit trouver une réponse
calculée par le code, ancrée et citée, sur les 100 lectures livrées — sans aucun appel au modèle."""

import json
import os

import pytest

from papertus import ask, i18n
from papertus.schema import Paper

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(ROOT, "data", "cache.json")
CHIPS = ["ask.chip.1", "ask.chip.2", "ask.chip.3", "ask.chip.4", "ask.chip.5"]


@pytest.fixture(scope="module")
def papers():
    if not os.path.exists(SEED):
        pytest.skip("data/cache.json (the shipped readings) is missing; run `make seed`")
    with open(SEED, encoding="utf-8") as fh:
        seed = json.load(fh)
    return sorted((Paper.from_dict(d) for d in seed.values()), key=lambda p: p.id)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    import papertus.llm as llm

    def boom(*a, **k):
        raise AssertionError("an example chip must be answered by the rules, never by the model")

    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(llm, "json_answer", boom)


@pytest.mark.parametrize("lang", i18n.LANGS)
@pytest.mark.parametrize("chip", CHIPS)
def test_every_chip_is_answered_by_the_rules(papers, lang, chip):
    question = i18n.t(chip, lang)
    assert question != chip, "missing chip string"
    a = ask.ask(question, papers, lang)
    assert a.by == "code", f"{lang} {question!r} fell through to {a.by}: {a.text}"
    assert a.grounded and a.cited_ids, f"{lang} {question!r} has no citation: {a.text}"
    assert all(i in a.text for i in a.cited_ids[:1]) or a.value is not None


def test_chips_agree_across_languages(papers):
    """La même pastille donne la même valeur dans les quatre langues (le code ne change pas avec la langue).
    La pastille 4 fait exception par choix : fr/de/en demandent le délai de l'Office des impôts (une date),
    it demande ce qui lui est encore dû (un montant)."""
    for chip in ("ask.chip.1", "ask.chip.2", "ask.chip.3", "ask.chip.5"):
        values = {lang: ask.ask(i18n.t(chip, lang), papers, lang).value for lang in i18n.LANGS}
        assert len({json.dumps(v) for v in values.values()}) == 1, f"{chip}: {values}"
    four = {lang: ask.ask(i18n.t("ask.chip.4", lang), papers, lang).value for lang in i18n.LANGS}
    assert four["fr"] == four["de"] == four["en"] and isinstance(four["fr"], str)   # une date ISO
    assert isinstance(four["it"], float) and four["it"] > 0                        # un montant


def test_institution_synonyms_cross_languages(papers):
    idx = ask.build_index(papers)
    ids = [i for i, _ in idx.search("Combien dois-je encore à la caisse-maladie ?")]
    kinds = {p.id: p.facts.get("letter_kind") for p in papers}
    # les avis de primes allemands et italiens sortent aussi, grâce aux synonymes de la classe
    hits = [i for i in ids if kinds.get(i) == "premium_notice"]
    langs = {p.language for p in papers if p.id in hits}
    assert len(hits) >= 3 and len(langs) >= 2, (ids, langs)
    assert ask.institution_in("Was schulde ich der Krankenkasse noch?") == "health"
    assert ask.institution_in("Qual è il termine dell'Ufficio esecuzioni?") == "debt"
    assert ask.institution_in("Quel est le solde à la banque ?") == "bank"
    assert ask.institution_in("Combien dois-je à Garage Morel SA ?") is None
