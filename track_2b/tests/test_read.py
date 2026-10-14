"""Lecture des PDF générés (si présents) et, avec une clé, un appel vivant au modèle."""

import glob
import os

import pytest

from paperasse import read

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPERS = sorted(glob.glob(os.path.join(ROOT, "data", "papers", "*.pdf")))
LIVE = pytest.mark.skipif(not os.getenv("LLM_API_KEY"), reason="LLM_API_KEY not set")


def test_classify_rules_without_model(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert read.classify("", "SPC\n0200\n1\n") == "qr_bill"
    assert read.classify("Kontoauszug September 2026\nAnfangssaldo 100.00\nSchlusssaldo 200.00", None) == "bank_statement"
    assert read.classify("Facture no 12 Montant CHF 10.00 Rechnung", None) == "qr_bill"
    assert read.classify("Office des poursuites, sommation de payer, décision", None) == "official_letter"


def test_guess_language():
    assert read.guess_language("Veuillez payer la facture pour le montant indiqué") == "fr"
    assert read.guess_language("Bitte bezahlen Sie die Rechnung mit dem Betrag") == "de"
    assert read.guess_language("Vogliate pagare la fattura per l'importo indicato della") == "it"


@pytest.mark.skipif(len(PAPERS) < 3, reason="no generated papers in data/papers")
def test_read_pdf_three_papers_and_qr_decodes():
    import json
    seen_qr = False
    for path in PAPERS[:12]:
        text, qr, pages = read.read_pdf(path)
        assert pages >= 1 and len(text) > 50
        truth_path = os.path.join(ROOT, "data", "truth", os.path.splitext(os.path.basename(path))[0] + ".json")
        if os.path.exists(truth_path):
            with open(truth_path, encoding="utf-8") as fh:
                truth = json.load(fh)
            if truth.get("kind") == "qr_bill" and "no_qr" not in truth.get("traps", []):
                assert qr is not None and qr.startswith("SPC"), path
                seen_qr = True
        if seen_qr and path == PAPERS[2]:
            break
    assert seen_qr, "no qr_bill with a QR among the first papers"


@LIVE
def test_live_extract_one_bill():
    text = "Facture no 2026-118\nLausanne, le 12.09.2026\nMontant total CHF 1'234.50\nPayable jusqu'au 12.10.2026"
    raw = read.extract("qr_bill", text, None)
    assert raw["amount_chf"] in (1234.5, "1234.50", "1'234.50")
    assert raw["invoice_number"] == "2026-118"


@LIVE
@pytest.mark.skipif(not PAPERS, reason="no generated papers")
def test_live_read_paper():
    paper = read.read_paper(PAPERS[0])
    assert paper.kind in ("qr_bill", "bank_statement", "official_letter")
    assert paper.facts.fields
