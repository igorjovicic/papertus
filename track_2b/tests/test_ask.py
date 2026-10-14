"""Les questions : règles en code, index FTS5, et aucun appel au modèle quand rien ne correspond."""

import os

import pytest

from paperasse import ask
from paperasse.schema import Facts, Field, Paper


def mk(pid, kind, lang, **values):
    fields = {k: Field(v, "verified", source="text") for k, v in values.items()}
    return Paper(id=pid, file=f"{pid}.pdf", kind=kind, language=lang,
                 text=" ".join(str(v) for v in values.values()), facts=Facts(fields=fields))


@pytest.fixture
def papers():
    return [
        mk("p001", "qr_bill", "fr", issuer="Fiduciaire Rochat SA", amount_chf=1234.5,
           invoice_date="2026-09-03", due_date="2026-10-03"),
        mk("p002", "qr_bill", "de", issuer="Garage Keller AG", amount_chf=420.0,
           invoice_date="2026-09-20", due_date="2026-10-20"),
        mk("p003", "qr_bill", "it", issuer="Fiduciaire Rochat SA", amount_chf=100.0,
           invoice_date="2026-08-20", due_date="2026-09-20"),
        mk("p004", "bank_statement", "fr", bank="Banque Alpina", account_iban="CH9300762011623852957",
           period_start="2026-09-01", period_end="2026-09-30", opening_balance=1000.0, closing_balance=900.0,
           movements=[{"date": "2026-09-05", "label": "Loyer", "amount": -100.0}]),
        mk("p005", "official_letter", "de", authority="Steuerverwaltung Kanton Bern", letter_date="2026-09-10",
           deadline="2026-10-31", amount_chf=2500.0, action_required="pay", subject="Veranlagung 2025"),
    ]


def test_rule_owed_to_issuer(papers):
    a = ask.answer_from_facts("Combien dois-je encore à Fiduciaire Rochat SA ?", papers, "fr")
    assert a is not None and a.by == "code" and a.grounded
    assert a.value == 1334.5 and sorted(a.cited_ids) == ["p001", "p003"]


def test_rule_balance_at_date(papers):
    a = ask.answer_from_facts("Quel est le solde du compte au 2026-09-30 ?", papers, "fr")
    assert a.value == 900.0 and a.cited_ids == ["p004"]
    papers[3].facts.fields["closing_balance"] = Field(None, "blank", reason="arithmetic")
    b = ask.answer_from_facts("Wie hoch ist der Saldo am 30.09.2026?", papers, "de")
    assert b.value is None and "arithmetic" in b.text and "leer" in b.text


def test_rule_due_on_date_lists_ids(papers):
    a = ask.answer_from_facts("Welche Rechnungen sind am 20. Oktober 2026 fällig?", papers, "de")
    assert a.value == ["p002"] and "Garage Keller AG" in a.text
    b = ask.answer_from_facts("Welche Rechnungen sind am 1. Januar 2026 fällig?", papers, "de")
    assert b.value == [] and b.cited_ids == []


def test_rule_sum_due_by_date(papers):
    a = ask.answer_from_facts("Combien dois-je payer jusqu'au 3 octobre 2026 ?", papers, "fr")
    assert a.value == 1334.5  # p003 (20.09) + p001 (03.10)


def test_rule_count_and_total_in_month(papers):
    a = ask.answer_from_facts("Quante fatture ho ricevuto in settembre?", papers, "it")
    assert a.value == 2 and "settembre 2026" in a.text
    b = ask.answer_from_facts("Total des factures reçues en septembre ?", papers, "fr")
    assert b.value == 1654.5
    c = ask.answer_from_facts("How many letters did I get in September 2026?", papers, "en")
    assert c.value == 1 and c.cited_ids == ["p005"]


def test_rule_deadline_of_authority(papers):
    a = ask.answer_from_facts("Quel est le délai de Steuerverwaltung Kanton Bern ?", papers, "fr")
    assert a.value == "2026-10-31" and a.cited_ids == ["p005"]


def test_no_rule_for_free_question(papers):
    assert ask.answer_from_facts("Qui a envoyé la lettre sur la Veranlagung ?", papers, "fr") is None


def test_fts_finds_the_right_paper(papers):
    idx = ask.build_index(papers)
    assert idx.search("Qui a envoyé la lettre sur la Veranlagung ?")[0][0] == "p005"
    assert idx.search("Garage Keller")[0][0] == "p002"
    assert idx.search("loyer banque alpina")[0][0] == "p004"


def test_query_terms_strip_stopwords():
    assert ask.query_terms("Combien dois-je encore à Fiduciaire Rochat SA ?") == ["fiduciaire", "rochat"]  # « sa » est un mot vide
    assert "2026" in ask.query_terms("Wie viel am 30.09.2026?")


def test_no_hit_makes_no_model_call(papers, monkeypatch):
    import paperasse.llm as llm

    def boom(*a, **k):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    a = ask.ask("zzzz qqqq", papers, "fr")
    assert a.by == "none" and a.text == "Aucun papier ne répond à cette question."


def test_model_answer_with_invented_number_is_replaced(papers, monkeypatch):
    import paperasse.llm as llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ("La lettre vient de Steuerverwaltung Kanton Bern [p005] pour CHF 7'777.00.", {"total_tokens": 10}))
    a = ask.ask("Qui a envoyé la lettre sur la Veranlagung ?", papers, "fr")
    assert a.grounded is False and a.text == "Je ne trouve pas ce chiffre dans vos papiers."
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ("La lettre vient de Steuerverwaltung Kanton Bern [p005], montant CHF 2'500.00.", {"total_tokens": 10}))
    b = ask.ask("Qui a envoyé la lettre sur la Veranlagung ?", papers, "fr")
    assert b.grounded is True and b.cited_ids == ["p005"] and b.by == "model"
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ("La lettre vient de la Steuerverwaltung.", {"total_tokens": 10}))
    c = ask.ask("Qui a envoyé la lettre sur la Veranlagung ?", papers, "fr")
    assert c.grounded is False  # pas de citation


@pytest.mark.skipif(not os.getenv("LLM_API_KEY"), reason="live model test, needs LLM_API_KEY")
def test_live_model_answer_cites(papers):
    a = ask.ask("Qui a envoyé la lettre sur la Veranlagung ?", papers, "fr")
    assert a.by == "model"
