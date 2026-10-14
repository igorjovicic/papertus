"""Tests du générateur : petit jeu dans un dossier temporaire, texte extractible, QR relisible."""

import json
import os

import pymupdf
import pytest
import zxingcpp
from PIL import Image

from paperasse import generate as g
from paperasse import swissqr
from paperasse.schema import DATE_KEYS, NUMERIC_KEYS, TRUTH_KEYS, Truth

SMALL_PLAN = {
    ("qr_bill", "fr"): 3, ("qr_bill", "de"): 3, ("qr_bill", "it"): 2,
    ("bank_statement", "fr"): 2, ("bank_statement", "de"): 2, ("bank_statement", "it"): 2,
    ("official_letter", "fr"): 3, ("official_letter", "de"): 2, ("official_letter", "it"): 1,
}


@pytest.fixture(scope="module")
def small_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    # Un seul relevé faux et trois factures sans QR pour ce petit jeu.
    g.UNBALANCED_STATEMENTS, saved_u = 1, g.UNBALANCED_STATEMENTS
    g.BILLS_WITHOUT_QR, saved_b = 3, g.BILLS_WITHOUT_QR
    try:
        truths = g.generate(str(out), seed=7, plan=SMALL_PLAN)
    finally:
        g.UNBALANCED_STATEMENTS, g.BILLS_WITHOUT_QR = saved_u, saved_b
    return out, truths


def _text(path: str) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def _decode_qr(path: str) -> str | None:
    with pymupdf.open(path) as doc:
        pix = doc[0].get_pixmap(dpi=150)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    found = zxingcpp.read_barcodes(img)
    return found[0].text if found else None


def test_counts_files_and_truth_shape(small_set):
    out, truths = small_set
    assert len(truths) == sum(SMALL_PLAN.values())
    assert sorted(os.listdir(out / "papers")) == sorted(f"{t.id}.pdf" for t in truths)
    assert sorted(os.listdir(out / "truth")) == sorted(f"{t.id}.json" for t in truths)
    for t in truths:
        with open(out / "truth" / f"{t.id}.json", encoding="utf-8") as fh:
            d = json.load(fh)
        assert set(d) >= {"id", "file", "kind", "language", "traps", "facts", "pages"}
        assert d["file"] == f"data/papers/{t.id}.pdf"
        assert list(d["facts"]) == TRUTH_KEYS[d["kind"]]
        assert Truth.from_dict(d).to_dict() == d
        for k, v in d["facts"].items():
            if v is None:
                continue
            if k in NUMERIC_KEYS:
                assert isinstance(v, float) and round(v, 2) == v
            if k in DATE_KEYS:
                assert len(v) == 10 and v[4] == "-" and v[7] == "-"
        if d["kind"] == "bank_statement":
            assert d["balanced"] in (True, False)
        else:
            assert "balanced" not in d


def test_text_is_extractable_and_holds_the_amount(small_set):
    out, truths = small_set
    for t in truths:
        text = _text(str(out / "papers" / f"{t.id}.pdf"))
        assert len(text) > 200, t.id
        f = t.facts
        amount = f.get("amount_chf") if t.kind != "bank_statement" else f["closing_balance"]
        if amount is None:
            assert "no_amount" in t.traps
            continue
        assert any(v in text for v in g.amount_variants(amount)), (t.id, amount)
        if t.kind == "qr_bill":
            assert f["invoice_number"] in text
            assert swissqr.iban_format(f["iban"]) in text
            assert f["recipient"] in text
        if t.kind == "bank_statement":
            assert any(v in text for v in g.amount_variants(f["opening_balance"]))
            for m in f["movements"]:
                assert m["label"] in text, (t.id, m)
                assert any(v in text for v in g.amount_variants(abs(m["amount"]))), (t.id, m)
        if t.kind == "official_letter":
            assert f["reference_number"] in text
            assert f["authority"] in text


def test_qr_round_trip_matches_truth(small_set):
    out, truths = small_set
    with_qr = [t for t in truths if t.kind == "qr_bill" and "no_qr" not in t.traps]
    without = [t for t in truths if t.kind == "qr_bill" and "no_qr" in t.traps]
    assert with_qr and len(without) == 3
    for t in with_qr:
        payload = _decode_qr(str(out / "papers" / f"{t.id}.pdf"))
        assert payload is not None, t.id
        bill = swissqr.parse_payload(payload)
        assert bill.is_valid(), (t.id, bill.problems())
        assert bill.iban == t.facts["iban"]
        assert bill.amount == pytest.approx(t.facts["amount_chf"])
        assert (bill.reference or None) == t.facts["reference"]
        assert bill.reference_type == t.facts["reference_type"]
        assert bill.creditor.name == t.facts["issuer"]
        assert bill.debtor.name == t.facts["recipient"]
        if t.facts["reference_type"] == "QRR":
            assert swissqr.is_qr_iban(bill.iban)
        else:
            assert not swissqr.is_qr_iban(bill.iban)
    for t in without:
        assert _decode_qr(str(out / "papers" / f"{t.id}.pdf")) is None
        assert t.facts["reference_type"] == "NON" and t.facts["reference"] is None
        assert swissqr.iban_valid(t.facts["iban"]) and not swissqr.is_qr_iban(t.facts["iban"])


def test_bank_statements_arithmetic_and_unbalanced_trap(small_set):
    _, truths = small_set
    statements = [t for t in truths if t.kind == "bank_statement"]
    unbalanced = [t for t in statements if not t.balanced]
    assert len(unbalanced) == 1 and "unbalanced" in unbalanced[0].traps
    for t in statements:
        f = t.facts
        computed = round(f["opening_balance"] + sum(m["amount"] for m in f["movements"]), 2)
        if t.balanced:
            assert computed == pytest.approx(f["closing_balance"])
        else:
            assert computed != pytest.approx(f["closing_balance"])
            assert t.expected_blank("closing_balance")
        assert f["period_start"] <= min(m["date"] for m in f["movements"])
        assert max(m["date"] for m in f["movements"]) <= f["period_end"]


def test_letters_cover_kinds_and_a_letter_has_no_amount(small_set):
    _, truths = small_set
    letters = [t for t in truths if t.kind == "official_letter"]
    assert {t.facts["letter_kind"] for t in letters} >= {"tax_decision", "payment_summons", "premium_notice", "commune_notice"}
    assert any(t.facts["amount_chf"] is None and "no_amount" in t.traps for t in letters)
    for t in letters:
        assert t.facts["deadline"] >= t.facts["letter_date"] or t.facts["letter_kind"] == "premium_notice"
        assert t.facts["action_required"] in ("pay", "reply", "submit_documents", "none")


def test_traps_and_formats_are_recorded(small_set):
    _, truths = small_set
    all_traps = {trap for t in truths for trap in t.traps}
    assert {"format_apostrophe", "format_space", "format_fr_dash"} & all_traps
    assert "no_qr" in all_traps and "unbalanced" in all_traps and "no_amount" in all_traps
    for t in truths:
        assert sum(1 for x in t.traps if x.startswith("format_")) == 1


def test_questions_file(small_set):
    out, truths = small_set
    with open(out / "questions.json", encoding="utf-8") as fh:
        qs = json.load(fh)
    ids = {t.id for t in truths}
    assert qs and len(qs) <= 50
    for q in qs:
        assert set(q) >= {"id", "lang", "question", "type", "truth", "papers"}
        assert q["lang"] in ("fr", "de", "it")
        assert q["type"] in ("amount", "date", "list", "count")
        assert set(q["papers"]) <= ids
        if q["type"] == "list":
            assert q["truth"] == q["papers"]
        if q["type"] == "count":
            assert q["truth"] == len(q["papers"])
        if q["type"] == "date":
            assert len(q["truth"]) == 10
    assert len({q["id"] for q in qs}) == len(qs)


def test_swiss_number_formats():
    assert g.num(1234.5, "apostrophe") == "1'234.50"
    assert g.num(1234.5, "space") == "1 234.50"
    assert g.num(1234.0, "fr_dash") == "1234.—"
    assert g.money(1234.5, "fr_dash") == "Fr. 1234.50"
    assert g.money(1234567.0, "space") == "CHF 1 234 567.00"
    assert "1'234.50" in g.amount_variants(1234.5) and "1234.50" in g.amount_variants(1234.5)


def test_generation_is_deterministic(tmp_path):
    a = g.generate(str(tmp_path / "a"), seed=3, plan={("qr_bill", "fr"): 2, ("bank_statement", "de"): 1, ("official_letter", "it"): 1})
    b = g.generate(str(tmp_path / "b"), seed=3, plan={("qr_bill", "fr"): 2, ("bank_statement", "de"): 1, ("official_letter", "it"): 1})
    assert [t.to_dict() for t in a] == [t.to_dict() for t in b]
