"""Les contrôles de code, cas faits à la main. Aucun appel au modèle ici."""

from paperasse import swissqr, verify
from paperasse.schema import Field

IBAN = swissqr.iban_build("CH", "00700123456789012")          # IBAN ordinaire valide
QR_IBAN = swissqr.iban_build("CH", "30000123456789012")       # QR-IBAN (clearing 30000)
QRR = swissqr.qrr_build("21000000000313947143000901")         # 27 chiffres
SCOR = swissqr.scor_build("539007547034")


def _payload(amount="1234.50", ref_type="QRR", ref=None, creditor="Boulangerie Vallotton SA"):
    bill = swissqr.QRBill(
        iban=QR_IBAN if ref_type == "QRR" else IBAN,
        creditor=swissqr.Address(name=creditor, street="Rue du Four", house_no="3", postal_code="1003", town="Lausanne"),
        amount=float(amount) if amount else None,
        currency="CHF",
        debtor=swissqr.Address(name="Marie Dupraz", street="Ch. des Lilas", house_no="12", postal_code="1004", town="Lausanne"),
        reference_type=ref_type,
        reference=ref if ref is not None else (QRR if ref_type == "QRR" else SCOR if ref_type == "SCOR" else ""),
        message="Facture 2026-118",
    )
    return bill.payload()


# --- nombres suisses ---------------------------------------------------------

def test_amounts_in_text_all_swiss_formats():
    text = "Total CHF 1'234.50, acompte 1 000.00, reste Fr. 234.—, frais 12.50 et 2’500.00 et 7777,25"
    found = verify.amounts_in_text(text)
    for v in (1234.50, 1000.00, 234.00, 12.50, 2500.00, 7777.25):
        assert v in found, v


def test_parse_amount_variants():
    assert verify.parse_amount("CHF 1'234.50") == 1234.5
    assert verify.parse_amount("1 234.50") == 1234.5
    assert verify.parse_amount("Fr. 1234.—") == 1234.0
    assert verify.parse_amount(1234) == 1234.0
    assert verify.parse_amount("-45.20") == -45.2
    assert verify.parse_amount(None) is None
    assert verify.parse_amount("n/a") is None


def test_normalise_number_text():
    assert verify.normalise_number_text("CHF 1'234.— et 1 000.50") == "CHF 1234.00 et 1000.50"


# --- checksums ---------------------------------------------------------------

def test_iban_mod97():
    assert verify.iban_valid(IBAN)
    assert verify.iban_valid(IBAN.lower().replace("CH", "ch"))
    assert verify.iban_valid("CH00 0070 0123 4567 8901 2".replace(" ", "")) is False
    assert verify.is_qr_iban(QR_IBAN) and not verify.is_qr_iban(IBAN)


def test_qrr_and_scor():
    assert verify.qrr_valid(QRR)
    assert not verify.qrr_valid(QRR[:-1] + str((int(QRR[-1]) + 1) % 10))
    assert verify.scor_valid(SCOR)
    assert not verify.scor_valid("RF00539007547034")


# --- dates -------------------------------------------------------------------

def test_dates_in_text_three_languages():
    text = "Lausanne, le 12 septembre 2026. Zahlbar bis 30.09.2026. Bellinzona, 1 ottobre 2026. Bern, 3. Oktober 2026. ISO 2026-10-15."
    found = {d.isoformat() for d in verify.dates_in_text(text)}
    assert {"2026-09-12", "2026-09-30", "2026-10-01", "2026-10-03", "2026-10-15"} <= found


def test_date_range_and_order():
    f = verify.verify("qr_bill", {"invoice_date": "2019-01-01", "due_date": "2026-09-30"}, "Zahlbar bis 30.09.2026", None)
    assert f.fields["invoice_date"].status == "blank" and "date" in f.fields["invoice_date"].reason
    assert f.fields["due_date"].status == "verified" and f.fields["due_date"].source == "text"
    g = verify.verify("qr_bill", {"invoice_date": "2026-09-30", "due_date": "2026-09-01"}, "30.09.2026 01.09.2026", None)
    assert g.fields["due_date"].status == "blank" and "before" in g.fields["due_date"].reason


# --- QR : le QR gagne --------------------------------------------------------

def test_qr_overrides_model():
    raw = {"issuer": "Boulangerie Valloton", "amount_chf": 1200.0, "iban": "CH00 0000", "reference": "1",
           "reference_type": "SCOR", "invoice_date": "2026-09-12", "due_date": "2026-10-12", "currency": "EUR"}
    f = verify.verify("qr_bill", raw, "Facture du 12.09.2026, à payer jusqu'au 12.10.2026, CHF 1'234.50", _payload())
    assert f.fields["amount_chf"].value == 1234.5 and f.fields["amount_chf"].source == "qr"
    assert "model said 1200.00" in f.fields["amount_chf"].reason
    assert f.fields["iban"].value == QR_IBAN and f.fields["iban"].status == "verified"
    assert f.fields["reference"].value == QRR and f.fields["reference_type"].value == "QRR"
    assert f.fields["issuer"].value == "Boulangerie Vallotton SA" and "model said" in f.fields["issuer"].reason
    assert f.fields["currency"].value == "CHF"
    assert f.fields["invoice_date"].status == "verified"


def test_qr_without_amount_falls_back_to_text():
    f = verify.verify("qr_bill", {"amount_chf": "CHF 89.90"}, "Montant CHF 89.90", _payload(amount=""))
    assert f.fields["amount_chf"].value == 89.9 and f.fields["amount_chf"].source == "text"


def test_qr_fields_reads_payload():
    q = verify.qr_fields(_payload(ref_type="SCOR"))
    assert q["iban"] == IBAN and q["reference_type"] == "SCOR" and q["reference"] == SCOR and q["amount"] == 1234.5
    assert verify.qr_fields(None) == {} and verify.qr_fields("hello") == {}


# --- facture sans QR ---------------------------------------------------------

def test_invoice_without_qr_checksums_decide():
    text = "Facture no F-2026-0042 du 05.09.2026. Montant total CHF 450.00. IBAN " + swissqr.iban_format(IBAN)
    f = verify.verify("qr_bill", {"iban": swissqr.iban_format(IBAN), "amount_chf": 450.0, "reference": None,
                                  "reference_type": "NON", "invoice_number": "F-2026-0042"}, text, None)
    assert f.fields["iban"].status == "verified" and f.fields["iban"].source == "iban"
    assert f.fields["amount_chf"].status == "verified" and f.fields["amount_chf"].source == "text"
    assert f.fields["invoice_number"].status == "verified"
    g = verify.verify("qr_bill", {"iban": "CH0000700123456789012", "amount_chf": 999.0}, text, None)
    assert g.fields["iban"].status == "blank" and "checksum" in g.fields["iban"].reason
    assert g.fields["amount_chf"].status == "model"


# --- relevé bancaire ---------------------------------------------------------

STATEMENT = """Relevé de compte 01.09.2026 - 30.09.2026
Solde initial 1'000.00
02.09.2026 Salaire +5'000.00
05.09.2026 Loyer -1'850.00
12.09.2026 Migros -123.45
Solde final 4'026.55
"""


def test_statement_arithmetic_balanced():
    raw = {"opening_balance": 1000.0, "closing_balance": 4026.55, "period_start": "2026-09-01", "period_end": "2026-09-30",
           "account_iban": IBAN, "movements": [
               {"date": "2026-09-02", "label": "Salaire", "amount": 5000.0},
               {"date": "2026-09-05", "label": "Loyer", "amount": -1850.0},
               {"date": "2026-09-12", "label": "Migros", "amount": -123.45}]}
    f = verify.verify("bank_statement", raw, STATEMENT, None)
    assert f.fields["closing_balance"].status == "verified" and f.fields["closing_balance"].source == "arithmetic"
    assert f.fields["balanced"].value is True
    assert f.fields["account_iban"].source == "iban"
    assert len(f.fields["movements"].value) == 3


def test_statement_arithmetic_unbalanced_blanks_closing():
    raw = {"opening_balance": 1000.0, "closing_balance": 4026.55, "movements": [
        {"date": "2026-09-02", "label": "Salaire", "amount": 5000.0},
        {"date": "2026-09-05", "label": "Loyer", "amount": -1850.0}]}
    f = verify.verify("bank_statement", raw, STATEMENT, None)
    assert f.fields["closing_balance"].status == "blank" and f.fields["closing_balance"].reason.startswith("arithmetic")
    assert f.fields["balanced"].value is False


def test_statement_movement_not_in_text_is_dropped():
    raw = {"opening_balance": 1000.0, "closing_balance": 4026.55, "movements": [
        {"date": "2026-09-02", "label": "Salaire", "amount": 5000.0},
        {"date": "2026-09-05", "label": "Loyer", "amount": -1850.0},
        {"date": "2026-09-12", "label": "Migros", "amount": -123.45},
        {"date": "2026-09-13", "label": "Invented", "amount": -0.01}]}
    f = verify.verify("bank_statement", raw, STATEMENT, None)
    assert len(f.fields["movements"].value) == 3 and "dropped" in f.fields["movements"].reason
    assert f.fields["closing_balance"].status == "blank"


def test_printed_totals_three_languages():
    assert verify.printed_totals("Belastung : Fr. 3182.70   Gutschrift : Fr. 0.—") == (3182.7, 0.0)
    assert verify.printed_totals("Débit : CHF 3 374.20   Crédit : CHF 6 158.00") == (3374.2, 6158.0)
    assert verify.printed_totals("Addebito : CHF 3'414.30   Accredito : CHF 195.50") == (3414.3, 195.5)
    assert verify.printed_totals("Solde final 4'026.55") is None


def test_statement_movement_totals_catch_an_ambiguous_sign_error():
    # Deux mouvements de 123.45 : si l'un perd son signe, le code ne peut pas dire lequel → blanc.
    text = STATEMENT + "13.09.2026 Coop +123.45\nDébit : CHF 1 973.45   Crédit : CHF 5 123.45\n"
    good = [{"date": "2026-09-02", "label": "Salaire", "amount": 5000.0},
            {"date": "2026-09-05", "label": "Loyer", "amount": -1850.0},
            {"date": "2026-09-12", "label": "Migros", "amount": -123.45},
            {"date": "2026-09-13", "label": "Coop", "amount": 123.45}]
    f = verify.verify("bank_statement", {"opening_balance": 1000.0, "closing_balance": 4150.00, "movements": good}, text, None)
    assert f.fields["movements"].status == "verified" and f.fields["closing_balance"].status == "verified"
    flipped = [dict(m) for m in good]
    flipped[2]["amount"] = 123.45  # le modèle a perdu le signe de Migros : deux candidats à 123.45
    g = verify.verify("bank_statement", {"opening_balance": 1000.0, "closing_balance": 4150.00, "movements": flipped}, text, None)
    assert g.fields["movements"].status == "blank" and g.fields["movements"].reason.startswith("movement_totals")
    assert g.fields["closing_balance"].status == "blank" and g.fields["closing_balance"].reason.startswith("arithmetic")


def test_statement_single_sign_flip_repaired_from_totals():
    text = STATEMENT + "Débit : CHF 1 973.45   Crédit : CHF 5 000.00\n"
    flipped = [{"date": "2026-09-02", "label": "Salaire", "amount": 5000.0},
               {"date": "2026-09-05", "label": "Loyer", "amount": -1850.0},
               {"date": "2026-09-12", "label": "Migros", "amount": 123.45}]
    f = verify.verify("bank_statement", {"opening_balance": 1000.0, "closing_balance": 4026.55, "movements": flipped}, text, None)
    assert f.fields["movements"].status == "verified" and "corrected" in f.fields["movements"].reason
    assert f.fields["movements"].value[2]["amount"] == -123.45
    assert f.fields["closing_balance"].status == "verified" and f.fields["balanced"].value is True


def test_iban_completed_from_text_when_model_drops_a_digit():
    text = "Relevé de compte\nIBAN " + swissqr.iban_format(IBAN) + "\nSolde initial 1.00"
    f = verify.verify("bank_statement", {"account_iban": IBAN[:-1]}, text, None)
    assert f.fields["account_iban"].value == IBAN and f.fields["account_iban"].status == "verified"
    assert "completed from text" in f.fields["account_iban"].reason
    g = verify.verify("bank_statement", {"account_iban": "CH00" + IBAN[4:]}, "no iban here", None)
    assert g.fields["account_iban"].status == "blank"


# --- lettre officielle -------------------------------------------------------

def test_letter_amount_must_be_in_text():
    text = "Office des poursuites. Sommation de payer. Délai : 15.10.2026. Réf. OP-2026-77."
    f = verify.verify("official_letter", {"amount_chf": 1500.0, "deadline": "2026-10-15", "letter_kind": "payment_summons",
                                          "reference_number": "OP-2026-77", "action_required": "pay"}, text, None)
    assert f.fields["amount_chf"].status == "blank" and "not found" in f.fields["amount_chf"].reason
    assert f.fields["deadline"].status == "verified"
    assert f.fields["reference_number"].status == "verified"
    g = verify.verify("official_letter", {"amount_chf": None, "letter_kind": "weird"}, text, None)
    assert g.fields["amount_chf"].status == "blank" and g.fields["letter_kind"].status == "blank"


def test_all_keys_present_for_each_kind():
    from paperasse.schema import TRUTH_KEYS
    for kind, keys in TRUTH_KEYS.items():
        f = verify.verify(kind, {}, "", None)
        assert set(keys) <= set(f.fields), kind
        assert all(fld.status == "blank" for k, fld in f.fields.items() if k in keys)


# --- ancrage de la prose -----------------------------------------------------

def test_numbers_grounded_ok():
    facts = [{"amount_chf": 1234.5, "due_date": "2026-09-30", "invoice_number": "F-2026-0042"}, {"closing_balance": 4026.55}]
    ok, missing = verify.numbers_grounded("Vous devez CHF 1'234.50 à payer jusqu'au 30.09.2026 (facture F-2026-0042) ; solde 4 026.55.", facts)
    assert ok, missing


def test_numbers_grounded_flags_invented():
    facts = {"amount_chf": Field(1234.5, "verified"), "due_date": "2026-09-30"}
    ok, missing = verify.numbers_grounded("Total 1'234.50 plus 99.00 de frais, échéance 2026-10-01.", facts)
    assert not ok and "99.00" in missing and "2026-10-01" in missing


def test_numbers_grounded_ignores_single_digits_and_citations_years():
    ok, missing = verify.numbers_grounded("3 factures en septembre 2026 [p012].", {"d": "2026-09-01", "id": "p012"})
    assert ok, missing


def test_numbers_grounded_accepts_id_suffix_but_not_inner_digits():
    facts = {"account_iban": "CH5208731722219063911", "closing_balance": 3138.75}
    ok, missing = verify.numbers_grounded("Le solde du compte se terminant par 3911 est de 3138.75 CHF.", facts)
    assert ok, missing
    ok, missing = verify.numbers_grounded("Le solde du compte 1722 est de 3138.75 CHF.", facts)
    assert not ok and "1722" in missing
