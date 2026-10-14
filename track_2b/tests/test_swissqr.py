"""Tests de swissqr : IBAN, QR-IBAN, références QRR et SCOR, charge utile aller-retour."""

import pytest

from paperasse import swissqr as q

# Vecteurs publics (exemples des spécifications et de l'ISO).
IBAN_OK = "CH93 0076 2011 6238 5295 7"
QR_IBAN_OK = "CH44 3199 9123 0008 8901 2"
QRR_OK = "210000000003139471430009017"
SCOR_OK = "RF18539007547034"


# --- IBAN ---------------------------------------------------------------------


def test_iban_valid_accepts_known_examples():
    assert q.iban_valid(IBAN_OK)
    assert q.iban_valid(QR_IBAN_OK)
    assert q.iban_valid("ch9300762011623852957")  # minuscules tolérées


def test_iban_valid_rejects_wrong_checksum_and_shapes():
    assert not q.iban_valid("CH94 0076 2011 6238 5295 7")
    assert not q.iban_valid("CH93 0076 2011 6238 5295")   # trop court
    assert not q.iban_valid("")
    assert not q.iban_valid("CHXX0076")
    assert not q.iban_valid("CH93 0076 2011 6238 5295 7!")


def test_iban_build_produces_valid_checksums():
    assert q.iban_build("CH", "00762011623852957") == "CH9300762011623852957"
    for bban in ("31999123000889012", "0023000000123456A", "0900000000000001"):
        iban = q.iban_build("CH", bban)
        assert q.iban_valid(iban), iban
        assert iban[4:] == bban.upper()


def test_iban_format_groups_of_four():
    assert q.iban_format("CH9300762011623852957") == "CH93 0076 2011 6238 5295 7"
    assert q.normalize_iban(" ch93-0076 2011 6238 5295 7 ") == "CH9300762011623852957"


def test_qr_iban_detection_uses_iid_range():
    assert q.iban_iid(QR_IBAN_OK) == 31999
    assert q.is_qr_iban(QR_IBAN_OK)
    assert not q.is_qr_iban(IBAN_OK)
    assert q.is_qr_iban(q.iban_build("CH", "30000" + "000000000001"))
    assert not q.is_qr_iban(q.iban_build("CH", "32000" + "000000000001"))
    assert not q.is_qr_iban(q.iban_build("CH", "29999" + "000000000001"))
    assert q.iban_iid("DE89370400440532013000") is None
    assert not q.is_qr_iban("DE89370400440532013000")


# --- QRR ----------------------------------------------------------------------


def test_mod10_recursive_known_values():
    assert q.mod10_recursive("21000000000313947143000901") == 7
    assert q.mod10_recursive("0") == 0
    with pytest.raises(q.SwissQRError):
        q.mod10_recursive("12a")


def test_qrr_build_and_valid():
    assert q.qrr_build("21000000000313947143000901") == QRR_OK
    assert q.qrr_valid(QRR_OK)
    assert q.qrr_valid("21 00000 00003 13947 14300 09017")
    assert not q.qrr_valid("210000000003139471430009018")
    assert not q.qrr_valid("21000000000313947143000901")   # 26 chiffres
    assert not q.qrr_valid("")
    short = q.qrr_build("42")
    assert len(short) == 27 and short.startswith("0" * 24 + "42") and q.qrr_valid(short)
    with pytest.raises(q.SwissQRError):
        q.qrr_build("1" * 27)


def test_qrr_format_groups():
    assert q.qrr_format(QRR_OK) == "21 00000 00003 13947 14300 09017"


# --- SCOR ---------------------------------------------------------------------


def test_scor_build_and_valid():
    assert q.scor_build("539007547034") == SCOR_OK
    assert q.scor_valid(SCOR_OK)
    assert q.scor_valid("RF18 5390 0754 7034")
    assert not q.scor_valid("RF19539007547034")
    assert not q.scor_valid("539007547034")
    assert q.scor_valid(q.scor_build("ABC123"))
    assert q.scor_format(SCOR_OK) == "RF18 5390 0754 7034"
    with pytest.raises(q.SwissQRError):
        q.scor_build("a" * 22)
    with pytest.raises(q.SwissQRError):
        q.scor_build("RF-1")


def test_reference_valid_by_type():
    assert q.reference_valid("QRR", QRR_OK)
    assert q.reference_valid("SCOR", SCOR_OK)
    assert q.reference_valid("NON", "")
    assert q.reference_valid("NON", None)
    assert not q.reference_valid("NON", "123")
    assert not q.reference_valid("QRR", SCOR_OK)
    assert not q.reference_valid("XYZ", QRR_OK)


# --- Charge utile -------------------------------------------------------------


def _bill(**over) -> q.QRBill:
    base = dict(
        iban="CH4431999123000889012",
        creditor=q.Address("Schreinerei Huber AG", "Bahnhofstrasse", "4", "8400", "Winterthur"),
        amount=1234.5,
        currency="CHF",
        debtor=q.Address("Anna Steiner", "Seestrasse", "12", "8002", "Zürich"),
        reference_type="QRR",
        reference=QRR_OK,
        message="Rechnung 2026-118",
    )
    base.update(over)
    return q.QRBill(**base)


def test_payload_layout_and_round_trip():
    bill = _bill()
    payload = bill.payload()
    lines = payload.split("\n")
    assert lines[0] == "SPC" and lines[1] == "0200" and lines[2] == "1"
    assert lines[3] == "CH4431999123000889012"
    assert lines[4] == "S" and lines[5] == "Schreinerei Huber AG" and lines[10] == "CH"
    assert lines[11:18] == [""] * 7
    assert lines[18] == "1234.50" and lines[19] == "CHF"
    assert lines[20] == "S" and lines[21] == "Anna Steiner"
    assert lines[27] == "QRR" and lines[28] == QRR_OK
    assert lines[29] == "Rechnung 2026-118" and lines[30] == "EPD"
    assert len(lines) == 31
    assert q.QRBill.parse(payload) == bill
    assert q.parse_payload(payload.replace("\n", "\r\n")) == bill
    assert q.looks_like_payload(payload)
    assert not q.looks_like_payload("Hello")


def test_payload_without_amount_or_debtor():
    bill = _bill(amount=None, debtor=q.Address(), reference_type="SCOR", reference=SCOR_OK,
                 iban="CH9300762011623852957")
    payload = bill.payload()
    lines = payload.split("\n")
    assert lines[18] == "" and lines[20:27] == [""] * 7
    back = q.QRBill.parse(payload)
    assert back.amount is None and back.debtor.is_empty()
    assert back.reference_type == "SCOR" and back.reference == SCOR_OK


def test_payload_with_billing_info_and_alt_procedures():
    bill = _bill(billing_info="//S1/10/2026-118/11/260901", alt_procedures=["eBill/B/abc", "twint/x"])
    lines = bill.payload().split("\n")
    assert lines[31] == "//S1/10/2026-118/11/260901"
    assert lines[32:34] == ["eBill/B/abc", "twint/x"]
    assert q.QRBill.parse(bill.payload()) == bill


def test_payload_refuses_inconsistent_bills():
    assert "QRR needs a QR-IBAN" in _bill(iban="CH9300762011623852957").problems()
    assert "SCOR needs a plain IBAN" in _bill(reference_type="SCOR", reference=SCOR_OK).problems()
    assert "NON must have no reference" in _bill(reference_type="NON", reference="1",
                                                  iban="CH9300762011623852957").problems()
    assert "QRR reference check digit" in _bill(reference=QRR_OK[:-1] + "8").problems()
    assert "iban checksum" in _bill(iban="CH4431999123000889013").problems()
    assert "currency" in _bill(currency="USD").problems()
    assert "amount range" in _bill(amount=0).problems()
    assert "creditor name" in _bill(creditor=q.Address()).problems()
    with pytest.raises(q.SwissQRError):
        _bill(currency="USD").payload()
    assert _bill().is_valid()


def test_parse_rejects_bad_payloads():
    with pytest.raises(q.SwissQRError):
        q.QRBill.parse("SPC\n0200\n1\n")
    bad = _bill().payload().replace("SPC", "SPX", 1)
    with pytest.raises(q.SwissQRError):
        q.QRBill.parse(bad)
    bad = _bill().payload().replace("\nEPD", "\nXXX")
    with pytest.raises(q.SwissQRError):
        q.QRBill.parse(bad)
    bad = _bill().payload().replace("\n0200\n", "\n0100\n")
    with pytest.raises(q.SwissQRError):
        q.QRBill.parse(bad)


def test_parse_combined_address_type_k():
    lines = _bill(debtor=q.Address()).payload().split("\n")
    lines[20:27] = ["K", "Luca Moretti", "Via Nassa 7", "6900 Lugano", "", "", "CH"]
    back = q.QRBill.parse("\n".join(lines))
    assert back.debtor.name == "Luca Moretti"
    assert back.debtor.postal_code == "6900" and back.debtor.town == "Lugano"


def test_address_paper_lines():
    a = q.Address("Boulangerie Dupraz Sàrl", "Rue du Lac", "12", "1800", "Vevey")
    assert a.paper_lines() == ["Boulangerie Dupraz Sàrl", "Rue du Lac 12", "1800 Vevey"]
    assert q.Address.from_lines(a.lines()) == a
    with pytest.raises(q.SwissQRError):
        q.Address.from_lines(["S", "x"])


def test_reference_for_paper():
    assert _bill().reference_for_paper() == "21 00000 00003 13947 14300 09017"
    assert _bill(reference_type="NON", reference="", iban=IBAN_OK).reference_for_paper() == ""
