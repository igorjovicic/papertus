"""Le rapport : tableaux par le code sur trois papiers faits main ; une prose inventée est filtrée."""

import os

import pytest

from paperasse import report
from paperasse.schema import Facts, Field, Paper


def mk(pid, kind, lang, **values):
    fields = {k: Field(v, "verified", source="text") for k, v in values.items()}
    return Paper(id=pid, file=f"{pid}.pdf", kind=kind, language=lang,
                 text=" ".join(str(v) for v in values.values()), facts=Facts(fields=fields))


@pytest.fixture
def papers():
    bill = mk("p001", "qr_bill", "fr", issuer="Fiduciaire Rochat SA", amount_chf=1234.5,
              invoice_date="2026-09-03", due_date="2026-10-03", currency="CHF")
    old_bill = mk("p002", "qr_bill", "de", issuer="Garage Keller AG", amount_chf=80.0,
                  invoice_date="2026-09-10", due_date="2026-08-30", currency="CHF")  # échéance avant le mois
    statement = mk("p003", "bank_statement", "fr", bank="Banque Alpina", account_iban="CH9300762011623852957",
                   period_start="2026-09-01", period_end="2026-09-30", opening_balance=1000.0,
                   closing_balance=850.0, movements=[{"date": "2026-09-05", "label": "Loyer", "amount": -200.0},
                                                     {"date": "2026-09-20", "label": "Salaire", "amount": 50.0}])
    letter = mk("p004", "official_letter", "it", authority="Ufficio delle imposte", subject="Decisione 2025",
                letter_date="2026-09-12", deadline="2026-10-12", amount_chf=300.0, action_required="pay")
    return [bill, old_bill, statement, letter]


def test_tables_are_built_by_code(papers):
    rep = report.build_report(papers, "2026-09", "fr", with_prose=False)
    assert [r.paper_id for r in rep.bills] == ["p002", "p001"]  # triées par échéance
    states = {r.paper_id: r.state for r in rep.bills}
    assert states["p001"] == "to_pay" and states["p002"] == "unknown"
    assert rep.totals["bills_total"] == 1314.5
    assert rep.totals["bills_to_pay"] == 1234.5
    acc = rep.accounts[0]
    assert (acc.inflow, acc.outflow, acc.closing, acc.balanced) == (50.0, 200.0, 850.0, True)
    assert rep.letters[0].deadline == "2026-10-12" and rep.letters[0].amount == 300.0


def test_unbalanced_statement_gets_a_warning(papers):
    papers[2].facts.fields["closing_balance"] = Field(900.0, "model")  # 1000 - 200 + 50 = 850, pas 900
    rep = report.build_report(papers, "2026-09", "de", with_prose=False)
    acc = rep.accounts[0]
    assert acc.balanced is False and acc.closing is None and "arithmetic" in acc.closing_reason
    md = report.render_markdown(rep, "de")
    assert "Schlusssaldo" in md and "ß" not in md
    html = report.render_html(rep, "de")
    assert "class='warn'" in html


def test_empty_month(papers):
    rep = report.build_report(papers, "2026-01", "it", with_prose=False)
    assert not rep.bills and not rep.accounts and not rep.letters
    assert "Nessun documento" in report.render_markdown(rep, "it")


def test_invented_number_sentence_is_dropped(papers):
    rep = report.build_report(papers, "2026-09", "fr", with_prose=False)
    tables = report.tables_json(rep)
    prose = "Vous avez reçu 2 factures pour CHF 1'314.50. Le compte termine à 850.00. Il reste 9'999.00 à régler."
    text, removed = report.filter_prose(prose, tables, "fr")
    assert removed == 1
    assert "9'999" not in text and "1'314.50" in text
    assert text.endswith("(une phrase retirée : chiffre non trouvé dans les papiers)")


def test_number_tokens_swiss_formats():
    assert report.number_tokens("CHF 1'234.50 et Fr. 12.— le 30.09.2026") == ["1234.50", "12", "30.09", "2026"]
    assert report.as_float("CHF 1 234.50") == 1234.5
    assert report.as_float("Fr. 1234.—") == 1234.0
    assert report.chf(1234567.891) == "1'234'567.89"


def test_status_line_reads_the_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("LLM_NAME", "swiss-ai/Apertus-v1.5-8B")
    assert report.status_line("fr") == "Apertus 8B · sur cette machine · rien ne sort"
    assert report.deployment()["local"] is True
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_NAME", "swiss-ai/Apertus-v1.5-70B")
    assert report.status_line("de") == "Apertus 70B · Schweizer Cloud" and "ß" not in report.status_line("de")
    monkeypatch.delenv("LLM_BASE_URL")
    assert report.status_line("it") == "Nessun modello configurato"


def test_renders_in_four_languages(papers):
    rep = report.build_report(papers, "2026-09", "fr", with_prose=False)
    for lang in ("fr", "de", "it", "en"):
        md = report.render_markdown(rep, lang)
        assert "p001" in md and "1'234.50" in md


@pytest.mark.skipif(not os.getenv("LLM_API_KEY"), reason="live model test, needs LLM_API_KEY")
def test_live_prose_is_grounded(papers):
    rep = report.build_report(papers, "2026-09", "fr")
    assert rep.prose
    assert len(rep.prose.split()) <= 120
