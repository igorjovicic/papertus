"""L'agenda : événements, groupes, chiffres, ligne du jour, état « fait », fichier .ics — code pur."""

from datetime import date, datetime, timezone

import pytest

from papertus import agenda
from papertus.schema import Facts, Field, Paper

TODAY = date(2026, 10, 1)


def mk(pid, kind, lang, status="verified", **values):
    fields = {k: Field(v, status, source="text") for k, v in values.items()}
    return Paper(id=pid, file=f"{pid}.pdf", kind=kind, language=lang, facts=Facts(fields=fields))


@pytest.fixture
def papers():
    ps = [
        mk("p001", "qr_bill", "fr", issuer="Gärtnerei Wyss", purpose="Jardin", amount_chf=2276.96, due_date="2026-10-03"),   # cette semaine
        mk("p002", "qr_bill", "de", issuer="Velo Studer AG", purpose="Velo", amount_chf=500.0, due_date="2026-09-20"),       # en retard
        mk("p003", "qr_bill", "it", issuer="Pasticceria Rossi", purpose="Torta", amount_chf=80.0, due_date="2026-10-20"),    # ce mois
        mk("p004", "qr_bill", "fr", issuer="Garage Morel SA", purpose="Service", amount_chf=900.0, due_date="2026-11-15"),  # plus tard
        mk("p005", "official_letter", "de", authority="Betreibungsamt Bern", subject="Zahlungsbefehl", amount_chf=1932.5,
           deadline="2026-10-05", action_required="pay"),                                                                  # cette semaine
        mk("p006", "official_letter", "fr", authority="Commune de Vevey", subject="Pièces", deadline="2026-10-26",
           action_required="submit_documents"),                                                                            # ce mois, sans montant
        mk("p007", "bank_statement", "fr", bank="Banque Alpina", account_iban="CH11", period_end="2026-09-30",
           opening_balance=100.0, closing_balance=900.0),                                                                   # passé, vérifié → fait
        mk("p008", "bank_statement", "fr", bank="Banque Alpina", account_iban="CH11", period_end="2026-08-31",
           opening_balance=50.0, closing_balance=100.0),                                                                    # plus ancien du même compte
        mk("p009", "bank_statement", "de", bank="Zugerland Bank AG", account_iban="CH22", period_end="2026-09-30",
           opening_balance=10.0),                                                                                           # solde blanc
    ]
    ps[8].facts.fields["closing_balance"] = Field(None, "blank", reason="arithmetic")
    return ps


def test_events_only_from_dated_fields(papers):
    evs = agenda.events_from_papers(papers)
    assert [e.id for e in evs] == ["p008:statement", "p002:bill", "p007:statement", "p009:statement", "p001:bill",
                                   "p005:deadline", "p003:bill", "p006:deadline", "p004:bill"]
    papers[0].facts.fields["due_date"] = Field(None, "blank", reason="not given")
    assert "p001:bill" not in {e.id for e in agenda.events_from_papers(papers)}


def test_grouping_and_done_state(papers):
    evs = agenda.events_from_papers(papers, done={"p002:bill"})
    groups = agenda.group_events(evs, TODAY)
    ids = {g: [e.id for e in v] for g, v in groups.items()}
    assert ids["overdue"] == ["p009:statement"]            # le relevé passé au solde blanc demande un regard
    assert ids["week"] == ["p001:bill", "p005:deadline"]
    assert ids["month"] == ["p003:bill", "p006:deadline"]
    assert ids["later"] == ["p004:bill"]
    assert ids["done"] == ["p008:statement", "p002:bill", "p007:statement"]  # fait, ou relevé passé vérifié


def test_figures(papers):
    evs = agenda.events_from_papers(papers)
    f = agenda.figures(evs, papers, TODAY)
    assert (f["due_7_total"], f["due_7_count"]) == (2276.96, 1)
    assert (f["overdue_total"], f["overdue_count"]) == (500.0, 1)      # p002 ; le relevé n'est pas un retard
    assert f["deadlines_month"] == 2
    assert (f["balance_total"], f["balance_blank"], f["balance_blank_ids"]) == (900.0, 1, ["p009"])  # dernier relevé par compte
    f2 = agenda.figures(agenda.events_from_papers(papers, done={"p002:bill"}), papers, TODAY)
    assert f2["overdue_count"] == 0


def test_today_line_four_languages(papers):
    evs = agenda.events_from_papers(papers)
    assert agenda.today_line(evs, TODAY, "fr") == "1 en retard, 1 facture à payer cette semaine (CHF 2'276.96), 1 délai, 1 relevé non vérifié."
    de = agenda.today_line(evs, TODAY, "de")
    assert de.startswith("1 überfällig, 1 Rechnung diese Woche zu zahlen (CHF 2'276.96), 1 Frist") and "ß" not in de
    assert "1 fattura da pagare questa settimana" in agenda.today_line(evs, TODAY, "it")
    assert "1 bill to pay this week" in agenda.today_line(evs, TODAY, "en")
    assert agenda.today_line([], TODAY, "fr") == "Rien à faire cette semaine."


def test_build_agenda_structure(papers):
    a = agenda.build_agenda(papers, TODAY, {"p002:bill"}, "it")
    assert a["today"] == "2026-10-01" and a["counts"]["done"] == 3 and a["groups"]["week"][0]["kind"] == "bill"
    assert a["groups"]["done"][1]["done"] is True


def test_ics_has_the_required_lines(papers):
    evs = agenda.events_from_papers(papers, done={"p002:bill"})
    agenda.group_events(evs, TODAY)
    ics = agenda.to_ics(evs, "fr", now=datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
    lines = ics.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and "VERSION:2.0" in lines and lines[-2] == "END:VCALENDAR" and lines[-1] == ""
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") == 8          # p002 est fait : pas de rappel
    assert "UID:p001:bill@papertus" in lines
    assert "DTSTART;VALUE=DATE:20261003" in lines and "DTSTAMP:20261001T080000Z" in lines
    assert "SUMMARY:Payer Gärtnerei Wyss · CHF 2'276.96" in lines
    assert "SUMMARY:Délai Betreibungsamt Bern · CHF 1'932.50" in lines
    assert any(l.startswith("SUMMARY:Relevé Zugerland Bank AG · non vérifié") for l in lines)
    assert ics.count("BEGIN:VALARM") == 8 and "TRIGGER:-P2D" in lines
    assert all(len(l.encode("utf-8")) <= 75 for l in lines)                   # lignes repliées
    assert "DESCRIPTION:Papertus [p006] · DÉLAI · Pièces · documents à fournir" in lines
    de = agenda.to_ics(evs, "de")
    assert "Unterlagen einreichen" in de and "submit_documents" not in de and "ß" not in de
