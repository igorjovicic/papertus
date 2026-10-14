"""Générateur de papiers suisses synthétiques : QR-factures, relevés bancaires, lettres officielles.

    python -m paperasse.generate --out track_2b/data --seed 42

Produit ``<out>/papers/pNNN.pdf``, ``<out>/truth/pNNN.json`` et ``<out>/questions.json``.
Tout est fictif : personnes, entreprises, adresses, banques, IBAN (contrôle valide).
Les PDF sont dessinés avec reportlab (texte réel, extractible) et le code QR avec segno.

Pièges plantés (notés dans ``traps`` de la vérité) : un second montant (acompte ou HT),
une date d'en-tête ou de pied de page étrangère, un nom de créancier qui dérive dans
le texte (le QR fait foi), cinq relevés dont les mouvements ne font pas le solde final,
des lettres sans aucun montant, des factures sans QR, et trois écritures de nombres
suisses (1'234.50 / CHF 1 234.50 / Fr. 1234.—).
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import random
from dataclasses import dataclass, field
from typing import Any

import segno
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .schema import Truth
from .swissqr import Address, QRBill, iban_build, iban_format, qrr_build, scor_build

PAGE_W, PAGE_H = A4
FONT = "Helvetica"
BOLD = "Helvetica-Bold"

# Répartition demandée : (genre, langue) → nombre de papiers.
DEFAULT_PLAN: dict[tuple[str, str], int] = {
    ("qr_bill", "fr"): 16, ("qr_bill", "de"): 16, ("qr_bill", "it"): 8,
    ("bank_statement", "fr"): 12, ("bank_statement", "de"): 12, ("bank_statement", "it"): 6,
    ("official_letter", "fr"): 12, ("official_letter", "de"): 12, ("official_letter", "it"): 6,
}
UNBALANCED_STATEMENTS = 5     # relevés faux exprès
BILLS_WITHOUT_QR = 8          # factures sans code QR (NON, IBAN dans le texte)


# =============================================================================
# Données fictives
# =============================================================================


@dataclass
class Company:
    name: str
    street: str
    no: str
    zip: str
    town: str
    vat: str
    drift: str                  # nom légèrement différent, pour le piège de dérive
    items: list[str]            # libellés de prestations dans la langue du papier


CREDITORS: dict[str, list[Company]] = {
    "fr": [
        Company("Boulangerie Dupraz Sàrl", "Rue du Lac", "12", "1800", "Vevey", "CHE-214.638.907",
                "Boulangerie Dupraz", ["Pain et viennoiseries – livraison", "Gâteau d'anniversaire", "Buffet apéritif 20 pers.", "Sandwichs – séminaire"]),
        Company("Garage Morel SA", "Avenue de Morges", "88", "1004", "Lausanne", "CHE-118.204.355",
                "Garage Morel", ["Service annuel", "Pneus hiver – montage", "Plaquettes de frein", "Contrôle anti-pollution"]),
        Company("Fiduciaire Lemaire Sàrl", "Rue de la Corraterie", "5", "1204", "Genève", "CHE-305.776.102",
                "Fiduciaire Lemaire & Associés", ["Tenue de comptabilité – trimestre", "Déclaration d'impôt 2025", "Bouclement annuel", "Conseil TVA"]),
        Company("Menuiserie Rochat", "Chemin des Peupliers", "3", "1400", "Yverdon-les-Bains", "CHE-427.913.580",
                "Menuiserie Rochat Sàrl", ["Pose de parquet chêne 24 m2", "Porte intérieure – fourniture", "Réparation fenêtre", "Étagères sur mesure"]),
        Company("Cabinet dentaire du Bourg", "Rue de Romont", "19", "1700", "Fribourg", "CHE-156.330.481",
                "Cabinet dentaire du Bourg SA", ["Contrôle et détartrage", "Radiographie panoramique", "Obturation composite", "Consultation"]),
        Company("Électricité Favre SA", "Route de Chavannes", "41", "1007", "Lausanne", "CHE-233.567.019",
                "Electricité Favre", ["Installation prises cuisine", "Remplacement tableau électrique", "Dépannage – déplacement", "Luminaires – pose"]),
        Company("Physio du Léman Sàrl", "Quai Perdonnet", "7", "1800", "Vevey", "CHE-371.802.644",
                "Physio du Léman", ["Physiothérapie 9 séances", "Massage thérapeutique", "Bilan postural", "Taping"]),
        Company("Jardins Vuille", "Rue des Terreaux", "14", "2000", "Neuchâtel", "CHE-492.118.736",
                "Jardins Vuille Sàrl", ["Taille de haie 45 m", "Tonte – abonnement été", "Plantation massif", "Évacuation déchets verts"]),
    ],
    "de": [
        Company("Schreinerei Huber AG", "Bahnhofstrasse", "4", "8400", "Winterthur", "CHE-109.447.213",
                "Schreinerei Huber", ["Einbauschrank nach Mass", "Reparatur Küchenfront", "Türe inkl. Montage", "Regal Eiche massiv"]),
        Company("Elektro Brunner GmbH", "Lorrainestrasse", "22", "3013", "Bern", "CHE-287.615.904",
                "Elektro Brunner", ["Installation Steckdosen", "Ersatz Sicherungskasten", "Störungsbehebung", "Leuchten montiert"]),
        Company("Praxis Dr. med. Keller", "Pilatusstrasse", "31", "6003", "Luzern", "CHE-342.880.117",
                "Praxis Keller", ["Konsultation", "Laboranalyse", "Impfung", "Verbandmaterial"]),
        Company("Treuhand Meier AG", "Baarerstrasse", "60", "6300", "Zug", "CHE-461.209.335",
                "Treuhand Meier", ["Buchhaltung Quartal", "Steuererklärung 2025", "Jahresabschluss", "MWST-Abrechnung"]),
        Company("Malerei Frei", "Rorschacherstrasse", "117", "9000", "St. Gallen", "CHE-178.553.062",
                "Malerei Frei GmbH", ["Wohnzimmer streichen 48 m2", "Fassade Teilsanierung", "Decke spachteln", "Material"]),
        Company("Velo Studer AG", "Seestrasse", "9", "8810", "Horgen", "CHE-520.714.288",
                "Velo Studer", ["Service komplett", "Bremsbeläge", "Kette und Kassette", "Licht vorne"]),
        Company("Gärtnerei Wyss", "Dorfstrasse", "55", "5000", "Aarau", "CHE-603.391.470",
                "Gärtnerei Wyss AG", ["Heckenschnitt 30 m", "Rasenpflege Abo", "Bepflanzung Rabatte", "Grüngut Entsorgung"]),
        Company("Sanitär Zbinden GmbH", "Thunstrasse", "140", "3074", "Muri bei Bern", "CHE-255.067.819",
                "Sanitär Zbinden", ["Boiler Ersatz", "Wasserhahn montiert", "Abfluss gereinigt", "Notfalleinsatz"]),
    ],
    "it": [
        Company("Studio Bianchi Sagl", "Via Nassa", "7", "6900", "Lugano", "CHE-398.420.156",
                "Studio Bianchi", ["Consulenza contabile trimestre", "Dichiarazione d'imposta 2025", "Chiusura annuale", "Consulenza IVA"]),
        Company("Pasticceria Rossi", "Viale Stazione", "18", "6500", "Bellinzona", "CHE-147.906.283",
                "Pasticceria Rossi Sagl", ["Torta nuziale", "Rinfresco 30 persone", "Pasticcini – consegna", "Panettoni"]),
        Company("Impresa Ferrari SA", "Via Cantonale", "102", "6600", "Locarno", "CHE-512.338.749",
                "Impresa Ferrari", ["Rifacimento bagno", "Piastrelle 32 m2", "Imbiancatura", "Trasporto materiale"]),
        Company("Fiduciaria Colombo", "Via Lavizzari", "4", "6850", "Mendrisio", "CHE-226.581.930",
                "Fiduciaria Colombo SA", ["Tenuta contabilità", "Dichiarazione fiscale", "Consulenza", "Pratiche amministrative"]),
    ],
}


@dataclass
class Person:
    name: str
    street: str
    no: str
    zip: str
    town: str
    employer: str = ""


RECIPIENTS: dict[str, list[Person]] = {
    "fr": [
        Person("Marie Favre", "Avenue de la Gare", "15", "1800", "Vevey", "Atelier Verdon SA"),
        Person("Jean-Luc Perrin", "Chemin du Levant", "8", "1005", "Lausanne", "Clinique du Parc"),
        Person("Sophie Berthoud", "Rue de Lyon", "72", "1203", "Genève", "Banque Romande du Lac SA"),
        Person("Nicolas Chappuis", "Rue du Collège", "3", "1400", "Yverdon-les-Bains", "Commune d'Yverdon"),
        Person("Camille Rossier", "Route de Berne", "28", "1700", "Fribourg", "Haute école fictive"),
        Person("Laurent Monnier", "Rue des Moulins", "11", "2000", "Neuchâtel", "Horlogerie Monnier"),
    ],
    "de": [
        Person("Anna Steiner", "Seestrasse", "12", "8002", "Zürich", "Steiner Consulting"),
        Person("Thomas Gerber", "Bümplizstrasse", "45", "3027", "Bern", "Bundesverwaltung"),
        Person("Lea Baumann", "Haldenstrasse", "7", "6006", "Luzern", "Spital Luzern"),
        Person("Martin Suter-Bircher", "Tösstalstrasse", "90", "8400", "Winterthur", "Werkstatt Suter"),
        Person("Nora Keller", "Chamerstrasse", "33", "6300", "Zug", "Alpenbank AG"),
        Person("David Frei", "Langgasse", "19", "9008", "St. Gallen", "Textil Frei AG"),
    ],
    "it": [
        Person("Luca Moretti", "Via Pretorio", "5", "6900", "Lugano", "Moretti Design"),
        Person("Giulia Pedrazzini", "Via Lugano", "27", "6500", "Bellinzona", "Cantone Ticino"),
        Person("Marco Bernasconi", "Via San Gottardo", "61", "6600", "Locarno", "Bernasconi & Figli"),
        Person("Elena Ferrari", "Via Turconi", "9", "6850", "Mendrisio", "Banca Sud Ticino SA"),
    ],
}


@dataclass
class Bank:
    name: str
    street: str
    zip: str
    town: str
    clearing: str          # 5 chiffres, hors plage QR-IBAN


BANKS: dict[str, list[Bank]] = {
    "fr": [
        Bank("Banque Romande du Lac SA", "Place Saint-François 2", "1003", "Lausanne", "08731"),
        Bank("Caisse d'Épargne de la Broye", "Rue de la Gare 9", "1530", "Payerne", "06412"),
        Bank("Banque du Jura Fictive SA", "Rue du Marché 4", "2800", "Delémont", "02498"),
    ],
    "de": [
        Bank("Alpenbank AG", "Bundesplatz 7", "3011", "Bern", "08305"),
        Bank("Sparkasse Tösstal", "Marktgasse 15", "8400", "Winterthur", "06876"),
        Bank("Zugerland Bank AG", "Bahnhofplatz 1", "6300", "Zug", "02219"),
    ],
    "it": [
        Bank("Banca Sud Ticino SA", "Piazza Riforma 3", "6900", "Lugano", "08127"),
        Bank("Cassa di Risparmio del Ceresio", "Via Nosetto 2", "6500", "Bellinzona", "06654"),
    ],
}

# Mouvements de compte : (libellé, min, max, signe). Les loyers et salaires sont mensuels.
MOVEMENTS: dict[str, list[tuple[str, float, float, int]]] = {
    "fr": [
        ("Migros Vevey", 8, 140, -1), ("Coop Pronto Lausanne", 5, 60, -1), ("SBB CFF FFS – billet", 3, 160, -1),
        ("Swisscom – facture", 59, 125, -1), ("Retrait Bancomat", 100, 400, -1), ("TWINT – Marché de Vevey", 8, 45, -1),
        ("Pharmacie du Centre", 12, 95, -1), ("Restaurant Le Lac", 28, 160, -1), ("Électricité – Services industriels", 55, 180, -1),
        ("Assurance-maladie Jura-Léman – prime", 280, 560, -1), ("Impôts – acompte", 300, 900, -1), ("Essence – station Rochat", 45, 110, -1),
        ("Virement reçu – remboursement", 20, 250, 1), ("TWINT reçu – partage repas", 10, 80, 1),
    ],
    "de": [
        ("Migros Winterthur", 8, 140, -1), ("Coop Bern Bahnhof", 5, 60, -1), ("SBB CFF FFS – Billett", 3, 160, -1),
        ("Swisscom – Rechnung", 59, 125, -1), ("Bezug Bancomat", 100, 400, -1), ("TWINT – Wochenmarkt", 8, 45, -1),
        ("Apotheke am Markt", 12, 95, -1), ("Restaurant Rössli", 28, 160, -1), ("Strom – Stadtwerke", 55, 180, -1),
        ("Krankenkasse Bergblick – Prämie", 280, 560, -1), ("Steuern – Akontozahlung", 300, 900, -1), ("Tankstelle Wyss", 45, 110, -1),
        ("Gutschrift – Rückzahlung", 20, 250, 1), ("TWINT Eingang – Essen geteilt", 10, 80, 1),
    ],
    "it": [
        ("Migros Lugano", 8, 140, -1), ("Coop Bellinzona", 5, 60, -1), ("FFS SBB CFF – biglietto", 3, 160, -1),
        ("Swisscom – fattura", 59, 125, -1), ("Prelievo Bancomat", 100, 400, -1), ("TWINT – mercato", 8, 45, -1),
        ("Farmacia Centrale", 12, 95, -1), ("Ristorante Al Lago", 28, 160, -1), ("Elettricità – Azienda elettrica", 55, 180, -1),
        ("Cassa malati Verbano – premio", 280, 560, -1), ("Imposte – acconto", 300, 900, -1), ("Benzina – stazione Ferrari", 45, 110, -1),
        ("Accredito – rimborso", 20, 250, 1), ("TWINT ricevuto – cena condivisa", 10, 80, 1),
    ],
}
RENT_LABEL = {"fr": "Loyer {month} – Régie du Lac", "de": "Miete {month} – Hausverwaltung Tösstal", "it": "Affitto {month} – Amministrazione immobiliare Ceresio"}
SALARY_LABEL = {"fr": "Salaire – {employer}", "de": "Lohn – {employer}", "it": "Stipendio – {employer}"}

MONTHS = {
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
}

# Autorités par langue et par genre de lettre : (nom, rue, NPA localité, canton, code).
AUTHORITIES: dict[str, dict[str, list[tuple[str, str, str, str]]]] = {
    "fr": {
        "tax_decision": [("Administration cantonale des impôts", "Route de Berne 46", "1014 Lausanne", "Canton de Vaud", "VD"),
                         ("Service cantonal des contributions", "Rue Joseph-Piller 13", "1701 Fribourg", "État de Fribourg", "FR")],
        "payment_summons": [("Office des poursuites du district de Lausanne", "Chemin de Mornex 32", "1014 Lausanne", "Canton de Vaud", "VD"),
                            ("Office des poursuites de la Riviera", "Rue du Simplon 22", "1800 Vevey", "Canton de Vaud", "VD")],
        "premium_notice": [("Assurance-maladie Jura-Léman SA", "Rue du Rhône 118", "1204 Genève", "Assureur LAMal", "GE")],
        "commune_notice": [("Commune de Vevey – Contrôle des habitants", "Rue du Simplon 18", "1800 Vevey", "Ville de Vevey", "VD"),
                           ("Commune d'Yverdon-les-Bains – Greffe municipal", "Place Pestalozzi 2", "1400 Yverdon-les-Bains", "Ville d'Yverdon", "VD")],
        "permit_decision": [("Commune de Vevey – Police des constructions", "Rue du Simplon 18", "1800 Vevey", "Ville de Vevey", "VD"),
                            ("Ville de Neuchâtel – Service de l'urbanisme", "Faubourg de l'Hôpital 4", "2000 Neuchâtel", "Ville de Neuchâtel", "NE")],
    },
    "de": {
        "tax_decision": [("Kantonales Steueramt", "Bändliweg 21", "8090 Zürich", "Kanton Zürich", "ZH"),
                         ("Steuerverwaltung des Kantons Bern", "Brünnenstrasse 66", "3018 Bern", "Kanton Bern", "BE")],
        "payment_summons": [("Betreibungsamt Winterthur", "Theaterstrasse 29", "8400 Winterthur", "Kanton Zürich", "ZH"),
                            ("Betreibungsamt Bern-Mittelland", "Poststrasse 25", "3071 Ostermundigen", "Kanton Bern", "BE")],
        "premium_notice": [("Krankenkasse Bergblick AG", "Alpenstrasse 9", "6004 Luzern", "KVG-Versicherer", "LU")],
        "commune_notice": [("Gemeinde Horgen – Einwohnerkontrolle", "Bahnhofstrasse 10", "8810 Horgen", "Gemeinde Horgen", "ZH"),
                           ("Stadt Winterthur – Einwohnerdienste", "Stadthausstrasse 21", "8400 Winterthur", "Stadt Winterthur", "ZH")],
        "permit_decision": [("Stadt Luzern – Bauverwaltung", "Hirschengraben 17", "6002 Luzern", "Stadt Luzern", "LU"),
                            ("Gemeinde Muri bei Bern – Bauabteilung", "Thunstrasse 74", "3074 Muri bei Bern", "Gemeinde Muri", "BE")],
    },
    "it": {
        "tax_decision": [("Ufficio delle imposte", "Viale Stefano Franscini 6", "6501 Bellinzona", "Repubblica e Cantone Ticino", "TI")],
        "payment_summons": [("Ufficio di esecuzione di Lugano", "Via Bossi 3", "6900 Lugano", "Repubblica e Cantone Ticino", "TI")],
        "premium_notice": [("Cassa malati Verbano SA", "Via Varenna 11", "6600 Locarno", "Assicuratore LAMal", "TI")],
        "commune_notice": [("Comune di Mendrisio – Controllo abitanti", "Via Municipio 13", "6850 Mendrisio", "Città di Mendrisio", "TI")],
        "permit_decision": [("Città di Lugano – Ufficio tecnico", "Via della Posta 8", "6900 Lugano", "Città di Lugano", "TI")],
    },
}

# Libellés de la section paiement (spécification, trois langues).
PAY_LABELS = {
    "fr": dict(receipt="Récépissé", part="Section paiement", account="Compte / Payable à", reference="Référence",
               info="Informations supplémentaires", payable_by="Payable par", payable_by_blank="Payable par (nom/adresse)",
               currency="Monnaie", amount="Montant", acceptance="Point de dépôt", detach="À détacher avant le versement"),
    "de": dict(receipt="Empfangsschein", part="Zahlteil", account="Konto / Zahlbar an", reference="Referenz",
               info="Zusätzliche Informationen", payable_by="Zahlbar durch", payable_by_blank="Zahlbar durch (Name/Adresse)",
               currency="Währung", amount="Betrag", acceptance="Annahmestelle", detach="Vor der Einzahlung abzutrennen"),
    "it": dict(receipt="Ricevuta", part="Sezione pagamento", account="Conto / Pagabile a", reference="Riferimento",
               info="Informazioni supplementari", payable_by="Pagabile da", payable_by_blank="Pagabile da (nome/indirizzo)",
               currency="Valuta", amount="Importo", acceptance="Punto di accettazione", detach="Da staccare prima del versamento"),
}

# Mots de la facture, du relevé et des lettres.
WORDS = {
    "fr": dict(invoice="Facture", invoice_no="Facture n°", date="Date", due="Échéance", customer_no="N° client", vat_no="N° TVA",
               description="Désignation", qty="Qté", unit="Prix unit.", total="Total", subtotal="Sous-total HT", vat="TVA 8.1 %",
               total_incl="Total TTC", deposit="Acompte reçu le {d}", to_pay="Montant à payer", terms="Payable à {n} jours, soit jusqu'au {d}.",
               thanks="Nous vous remercions de votre confiance.", vat_included="TVA 8.1 % incluse",
               transfer="Paiement par virement bancaire sur le compte ci-dessous, en mentionnant le numéro de facture.",
               bank="Banque", holder="Titulaire", footer_terms="Conditions générales de vente du {d}", page="Page {p}/{n}",
               statement="Relevé de compte", account="Compte privé CHF", period="Période du {a} au {b}", iban="IBAN",
               opening="Solde initial au {d}", closing="Solde final au {d}", col_date="Date", col_label="Libellé", col_debit="Débit",
               col_credit="Crédit", col_balance="Solde", e_statement="Relevé établi électroniquement, valable sans signature.",
               printed="Imprimé le {d}", holder_label="Titulaire du compte", ref="Réf.", your_ref="Votre contact",
               appeal="Voies de droit : la présente décision peut faire l'objet d'une réclamation écrite dans les 30 jours dès sa notification.",
               greeting="Madame, Monsieur,", closing_formula="Veuillez agréer, Madame, Monsieur, nos salutations distinguées.",
               phone="Tél. 0XX 000 00 00", form_version="Formulaire version {d}"),
    "de": dict(invoice="Rechnung", invoice_no="Rechnung Nr.", date="Datum", due="Fällig am", customer_no="Kunden-Nr.", vat_no="MWST-Nr.",
               description="Bezeichnung", qty="Menge", unit="Einzelpreis", total="Total", subtotal="Zwischentotal exkl. MWST", vat="MWST 8.1 %",
               total_incl="Total inkl. MWST", deposit="Anzahlung erhalten am {d}", to_pay="Zu zahlender Betrag", terms="Zahlbar innert {n} Tagen, bis am {d}.",
               thanks="Wir danken Ihnen für Ihr Vertrauen.", vat_included="inkl. 8.1 % MWST",
               transfer="Zahlung per Banküberweisung auf das untenstehende Konto, bitte Rechnungsnummer angeben.",
               bank="Bank", holder="Kontoinhaber", footer_terms="Allgemeine Geschäftsbedingungen vom {d}", page="Seite {p}/{n}",
               statement="Kontoauszug", account="Privatkonto CHF", period="Periode vom {a} bis {b}", iban="IBAN",
               opening="Anfangssaldo per {d}", closing="Schlusssaldo per {d}", col_date="Datum", col_label="Buchungstext", col_debit="Belastung",
               col_credit="Gutschrift", col_balance="Saldo", e_statement="Elektronisch erstellter Auszug, gültig ohne Unterschrift.",
               printed="Gedruckt am {d}", holder_label="Kontoinhaber", ref="Ref.-Nr.", your_ref="Ihre Kontaktperson",
               appeal="Rechtsmittel: Gegen diese Verfügung kann innert 30 Tagen seit Zustellung schriftlich Einsprache erhoben werden.",
               greeting="Sehr geehrte Damen und Herren", closing_formula="Freundliche Grüsse",
               phone="Tel. 0XX 000 00 00", form_version="Formular Version {d}"),
    "it": dict(invoice="Fattura", invoice_no="Fattura n.", date="Data", due="Scadenza", customer_no="N. cliente", vat_no="N. IVA",
               description="Descrizione", qty="Qtà", unit="Prezzo unit.", total="Totale", subtotal="Subtotale IVA esclusa", vat="IVA 8.1 %",
               total_incl="Totale IVA inclusa", deposit="Acconto ricevuto il {d}", to_pay="Importo da pagare", terms="Pagabile entro {n} giorni, ossia entro il {d}.",
               thanks="Vi ringraziamo per la fiducia.", vat_included="IVA 8.1 % inclusa",
               transfer="Pagamento tramite bonifico bancario sul conto sottostante, indicando il numero di fattura.",
               bank="Banca", holder="Intestatario", footer_terms="Condizioni generali di vendita del {d}", page="Pagina {p}/{n}",
               statement="Estratto conto", account="Conto privato CHF", period="Periodo dal {a} al {b}", iban="IBAN",
               opening="Saldo iniziale al {d}", closing="Saldo finale al {d}", col_date="Data", col_label="Descrizione", col_debit="Addebito",
               col_credit="Accredito", col_balance="Saldo", e_statement="Estratto generato elettronicamente, valido senza firma.",
               printed="Stampato il {d}", holder_label="Intestatario del conto", ref="Rif.", your_ref="Persona di contatto",
               appeal="Rimedi giuridici: contro la presente decisione può essere presentato reclamo scritto entro 30 giorni dalla notifica.",
               greeting="Gentili Signore e Signori,", closing_formula="Distinti saluti.",
               phone="Tel. 0XX 000 00 00", form_version="Modulo versione {d}"),
}


# =============================================================================
# Formats suisses
# =============================================================================

STYLES = ("apostrophe", "space", "fr_dash")


def num(x: float, style: str) -> str:
    """Nombre sans monnaie : 1'234.50 / 1 234.50 / 1234.— selon le style."""
    x = round(abs(x), 2)
    whole = int(x)
    cents = int(round((x - whole) * 100))
    if style == "apostrophe":
        return f"{whole:,}".replace(",", "'") + f".{cents:02d}"
    if style == "space":
        return f"{whole:,}".replace(",", " ") + f".{cents:02d}"
    return f"{whole}." + ("—" if cents == 0 else f"{cents:02d}")


def signed_num(x: float, style: str) -> str:
    """Solde avec son signe : -1'353.90 (les mouvements, eux, vont en colonne débit/crédit)."""
    return ("-" if round(x, 2) < 0 else "") + num(x, style)


def money(x: float, style: str) -> str:
    """Montant avec monnaie : CHF 1'234.50 / CHF 1 234.50 / Fr. 1234.—"""
    prefix = "Fr." if style == "fr_dash" else "CHF"
    return f"{prefix} {num(x, style)}"


def amount_variants(x: float) -> list[str]:
    """Toutes les écritures qu'un papier peut porter pour ce montant (pour les tests)."""
    return sorted({signed_num(x, s) for s in STYLES} | {f"{round(x, 2):.2f}"})


def fmt_date(d: dt.date, lang: str, long: bool = False) -> str:
    if not long:
        return d.strftime("%d.%m.%Y")
    month = MONTHS[lang][d.month - 1]
    if lang == "de":
        return f"{d.day}. {month} {d.year}"
    if lang == "fr":
        return f"{d.day}{'er' if d.day == 1 else ''} {month} {d.year}"
    return f"{d.day} {month} {d.year}"


def date_line(town: str, d: dt.date, lang: str) -> str:
    if lang == "fr":
        return f"{town}, le {fmt_date(d, lang, True)}"
    return f"{town}, {fmt_date(d, lang, True)}"


# =============================================================================
# Feuille : petit confort au-dessus du canevas reportlab
# =============================================================================


class Sheet:
    """Une page A4 avec un curseur vertical (en mm depuis le haut)."""

    def __init__(self, path: str, title: str):
        self.c = canvas.Canvas(path, pagesize=A4, invariant=1)   # md5 stable d'un run à l'autre
        self.c.setTitle(title)
        self.c.setAuthor("Paperasse – papiers synthétiques")
        self.c.setSubject("Document fictif généré pour un jeu de test")
        self.pages = 1

    # -- primitives ----------------------------------------------------------
    def y(self, top_mm: float) -> float:
        return PAGE_H - top_mm * mm

    def text(self, x_mm: float, top_mm: float, s: str, size: float = 10, bold: bool = False, right: bool = False) -> None:
        self.c.setFont(BOLD if bold else FONT, size)
        if right:
            self.c.drawRightString(x_mm * mm, self.y(top_mm), s)
        else:
            self.c.drawString(x_mm * mm, self.y(top_mm), s)

    def lines(self, x_mm: float, top_mm: float, items: list[str], size: float = 10, bold: bool = False, leading: float | None = None) -> float:
        lead = leading or (size * 0.42)   # en mm
        for i, s in enumerate(items):
            self.text(x_mm, top_mm + i * lead, s, size, bold)
        return top_mm + len(items) * lead

    def paragraph(self, x_mm: float, top_mm: float, s: str, width_mm: float, size: float = 10, bold: bool = False) -> float:
        """Texte coupé aux mots ; rend la position après le paragraphe."""
        font = BOLD if bold else FONT
        words = s.split(" ")
        out: list[str] = []
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if stringWidth(trial, font, size) <= width_mm * mm:
                cur = trial
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
        return self.lines(x_mm, top_mm, out, size, bold, leading=size * 0.45)

    def rule(self, x1: float, x2: float, top_mm: float, width: float = 0.5, dashed: bool = False) -> None:
        self.c.setLineWidth(width)
        if dashed:
            self.c.setDash(3, 2)
        self.c.line(x1 * mm, self.y(top_mm), x2 * mm, self.y(top_mm))
        self.c.setDash()

    def vrule(self, x: float, top1: float, top2: float, width: float = 0.5, dashed: bool = False) -> None:
        self.c.setLineWidth(width)
        if dashed:
            self.c.setDash(3, 2)
        self.c.line(x * mm, self.y(top1), x * mm, self.y(top2))
        self.c.setDash()

    def box(self, x: float, top_mm: float, w: float, h: float, fill: float | None = None) -> None:
        if fill is not None:
            self.c.setFillGray(fill)
            self.c.rect(x * mm, self.y(top_mm + h), w * mm, h * mm, stroke=0, fill=1)
            self.c.setFillGray(0)
        else:
            self.c.setLineWidth(0.4)
            self.c.rect(x * mm, self.y(top_mm + h), w * mm, h * mm, stroke=1, fill=0)

    def new_page(self) -> None:
        self.c.showPage()
        self.pages += 1

    def save(self) -> None:
        self.c.save()


# =============================================================================
# Code QR et section paiement
# =============================================================================


def qr_image(payload: str) -> ImageReader:
    """Le code QR (niveau M, comme l'exige la norme) en image mémoire."""
    qr = segno.make(payload, error="m", micro=False)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=0)
    buf.seek(0)
    return ImageReader(buf)


def draw_swiss_cross(sheet: Sheet, cx_mm: float, cy_mm: float) -> None:
    """La croix suisse au centre du code (carré noir 7 mm, croix blanche)."""
    c = sheet.c
    size = 7 * mm
    c.setFillGray(1)
    c.rect(cx_mm * mm - size / 2 - 0.4 * mm, cy_mm * mm - size / 2 - 0.4 * mm, size + 0.8 * mm, size + 0.8 * mm, stroke=0, fill=1)
    c.setFillGray(0)
    c.rect(cx_mm * mm - size / 2, cy_mm * mm - size / 2, size, size, stroke=0, fill=1)
    arm_w, arm_l = 1.2 * mm, 4.4 * mm
    c.setFillGray(1)
    c.rect(cx_mm * mm - arm_w / 2, cy_mm * mm - arm_l / 2, arm_w, arm_l, stroke=0, fill=1)
    c.rect(cx_mm * mm - arm_l / 2, cy_mm * mm - arm_w / 2, arm_l, arm_w, stroke=0, fill=1)
    c.setFillGray(0)


def draw_payment_part(sheet: Sheet, bill: QRBill, lang: str, payload: str) -> None:
    """Récépissé (62 mm) + section paiement (148 mm) sur les 105 mm du bas de la page."""
    L = PAY_LABELS[lang]
    top = 297 - 105                              # bord supérieur de la section, en mm depuis le haut
    sheet.rule(0, 210, top, 0.3, dashed=True)
    sheet.vrule(62, top, 297, 0.3, dashed=True)
    sheet.text(62 + 2, top - 1.2, L["detach"], 7)

    amount_s = "" if bill.amount is None else num(bill.amount, "space")
    account_lines = [iban_format(bill.iban)] + bill.creditor.paper_lines()
    ref_line = bill.reference_for_paper()
    debtor_lines = bill.debtor.paper_lines() if not bill.debtor.is_empty() else []

    # --- Récépissé (gauche) ---
    x = 5
    y = top + 10
    sheet.text(x, y, L["receipt"], 11, bold=True)
    y += 7
    sheet.text(x, y, L["account"], 6, bold=True); y += 3.2
    y = sheet.lines(x, y, account_lines, 8, leading=3.4) + 1.5
    if ref_line:
        sheet.text(x, y, L["reference"], 6, bold=True); y += 3.2
        sheet.text(x, y, ref_line, 8); y += 4.5
    if debtor_lines:
        sheet.text(x, y, L["payable_by"], 6, bold=True); y += 3.2
        y = sheet.lines(x, y, debtor_lines, 8, leading=3.4)
    else:
        sheet.text(x, y, L["payable_by_blank"], 6, bold=True); y += 3.2
        sheet.box(x, y, 52, 20)
    ya = top + 105 - 37
    sheet.text(x, ya, L["currency"], 6, bold=True)
    sheet.text(x + 20, ya, L["amount"], 6, bold=True)
    sheet.text(x, ya + 3.5, bill.currency, 8)
    if amount_s:
        sheet.text(x + 20, ya + 3.5, amount_s, 8)
    else:
        sheet.box(x + 20, ya + 1, 30, 10)
    sheet.text(57, top + 105 - 15, L["acceptance"], 6, bold=True, right=True)

    # --- Section paiement (droite) ---
    x = 67
    sheet.text(x, top + 10, L["part"], 11, bold=True)
    qr_top = top + 17
    qr_size = 46
    sheet.c.drawImage(qr_image(payload), x * mm, sheet.y(qr_top + qr_size), qr_size * mm, qr_size * mm)
    draw_swiss_cross(sheet, x + qr_size / 2, 297 - (qr_top + qr_size / 2))
    ya = qr_top + qr_size + 7
    sheet.text(x, ya, L["currency"], 8, bold=True)
    sheet.text(x + 22, ya, L["amount"], 8, bold=True)
    sheet.text(x, ya + 4.5, bill.currency, 10)
    if amount_s:
        sheet.text(x + 22, ya + 4.5, amount_s, 10)
    else:
        sheet.box(x + 22, ya + 1.5, 40, 15)

    xi = 118
    y = top + 10
    sheet.text(xi, y, L["account"], 8, bold=True); y += 4
    y = sheet.lines(xi, y, account_lines, 10, leading=4.2) + 2
    if ref_line:
        sheet.text(xi, y, L["reference"], 8, bold=True); y += 4
        sheet.text(xi, y, ref_line, 10); y += 6
    if bill.message:
        sheet.text(xi, y, L["info"], 8, bold=True); y += 4
        y = sheet.paragraph(xi, y, bill.message, 82, 10) + 2
    if debtor_lines:
        sheet.text(xi, y, L["payable_by"], 8, bold=True); y += 4
        sheet.lines(xi, y, debtor_lines, 10, leading=4.2)
    else:
        sheet.text(xi, y, L["payable_by_blank"], 8, bold=True); y += 4
        sheet.box(xi, y, 65, 25)


# =============================================================================
# Génération des trois genres
# =============================================================================


@dataclass
class Spec:
    pid: str
    kind: str
    lang: str
    style: str
    flags: set[str] = field(default_factory=set)   # pièges décidés avant le dessin


def rand_cents(rng: random.Random, lo: float, hi: float) -> float:
    cents = rng.choice([0, 0, 0, 50, 20, 85, 30, 75, 10, 90, 45])
    return round(rng.randint(int(lo), int(hi)) + cents / 100, 2)


def rand_date(rng: random.Random, start: dt.date, end: dt.date) -> dt.date:
    return start + dt.timedelta(days=rng.randint(0, (end - start).days))


INVOICE_DATES = [dt.date(2026, 8, 3) + dt.timedelta(days=3 * i) for i in range(25)]   # 03.08 … 14.10, pas de 3 jours


def make_qr_bill(spec: Spec, rng: random.Random, out: str) -> Truth:
    lang, style = spec.lang, spec.style
    W = WORDS[lang]
    creditor = rng.choice(CREDITORS[lang])
    debtor = rng.choice(RECIPIENTS[lang])
    invoice_date = rng.choice(INVOICE_DATES)
    terms_days = rng.choice([30, 30, 30, 20, 10])
    due_date = invoice_date + dt.timedelta(days=terms_days)
    invoice_no = f"{invoice_date.year}-{rng.randint(100, 999)}"
    customer_no = f"{rng.randint(1000, 9999)}"

    # Lignes de prestations → HT → TVA → TTC.
    n_items = rng.randint(1, 4)
    items = []
    for label in rng.sample(creditor.items, n_items):
        qty = rng.choice([1, 1, 1, 2, 3])
        unit = rand_cents(rng, 45, 1800)
        items.append((label, qty, unit, round(qty * unit, 2)))
    ht = round(sum(it[3] for it in items), 2)
    vat = round(ht * 0.081, 2)
    ttc = round(ht + vat, 2)
    traps: list[str] = [f"format_{style}"]

    deposit = None
    show_breakdown = "second_amount" in spec.flags
    if show_breakdown and rng.random() < 0.5:
        deposit = round(ttc * rng.choice([0.3, 0.4, 0.5]), 2)
        deposit = float(int(deposit))                   # un acompte rond
    amount = round(ttc - (deposit or 0.0), 2)
    if show_breakdown:
        traps.append("second_amount")

    no_qr = "no_qr" in spec.flags
    if no_qr:
        reference_type = "NON"
        bank = rng.choice(BANKS[lang])
        iban = iban_build("CH", bank.clearing + f"{rng.randint(0, 999999999999):012d}")
        reference = None
        traps.append("no_qr")
    else:
        reference_type = rng.choices(["QRR", "SCOR", "NON"], weights=[6, 2, 1])[0]
        if reference_type == "QRR":
            iban = iban_build("CH", f"{rng.randint(30000, 31999)}" + f"{rng.randint(0, 999999999999):012d}")
            reference = qrr_build(f"{rng.randint(10, 99)}{rng.randint(0, 10**20 - 1):020d}{int(invoice_no.replace('-', '')[-4:])}")
        else:
            bank = rng.choice(BANKS[lang])
            iban = iban_build("CH", bank.clearing + f"{rng.randint(0, 999999999999):012d}")
            reference = scor_build(invoice_no.replace("-", "") + f"{rng.randint(0, 9999):04d}") if reference_type == "SCOR" else None

    drift = "creditor_name_drift" in spec.flags and not no_qr
    header_name = creditor.drift if drift else creditor.name
    if drift:
        traps.append("creditor_name_drift")
    footer_date = "footer_date" in spec.flags
    if footer_date:
        traps.append("footer_date")

    purpose = f"{W['invoice']} {invoice_no}"
    bill = QRBill(
        iban=iban,
        creditor=Address(creditor.name, creditor.street, creditor.no, creditor.zip, creditor.town),
        amount=amount, currency="CHF",
        debtor=Address(debtor.name, debtor.street, debtor.no, debtor.zip, debtor.town),
        reference_type=reference_type, reference=reference or "", message=purpose,
    )
    payload = None if no_qr else bill.payload()

    # --- dessin ---
    path = os.path.join(out, "papers", f"{spec.pid}.pdf")
    sh = Sheet(path, f"{W['invoice']} {invoice_no}")
    sh.text(20, 20, header_name, 14, bold=True)
    sh.lines(20, 26, [f"{creditor.street} {creditor.no}", f"{creditor.zip} {creditor.town}", f"{W['vat_no']} {creditor.vat} TVA" if lang == "fr" else f"{W['vat_no']} {creditor.vat} {'MWST' if lang == 'de' else 'IVA'}"], 9)
    sh.rule(20, 190, 40, 0.6)
    # Bloc adresse du destinataire (fenêtre à droite).
    sh.lines(120, 48, [debtor.name, f"{debtor.street} {debtor.no}", f"{debtor.zip} {debtor.town}"], 10)
    sh.text(20, 70, date_line(creditor.town, invoice_date, lang), 10)
    sh.text(20, 82, f"{W['invoice_no']} {invoice_no}", 13, bold=True)
    sh.lines(120, 82, [f"{W['date']} : {fmt_date(invoice_date, lang)}", f"{W['customer_no']} : {customer_no}", f"{W['due']} : {fmt_date(due_date, lang)}"], 9, leading=4.2)

    y = 98
    sh.text(20, y, W["description"], 9, bold=True)
    sh.text(130, y, W["qty"], 9, bold=True, right=True)
    sh.text(160, y, W["unit"], 9, bold=True, right=True)
    sh.text(190, y, W["total"], 9, bold=True, right=True)
    sh.rule(20, 190, y + 1.5, 0.4)
    y += 7
    for label, qty, unit, total in items:
        sh.text(20, y, label, 10)
        sh.text(130, y, str(qty), 10, right=True)
        sh.text(160, y, num(unit, style), 10, right=True)
        sh.text(190, y, num(total, style), 10, right=True)
        y += 6
    sh.rule(110, 190, y - 2, 0.4)
    y += 3
    if show_breakdown:
        sh.text(120, y, W["subtotal"], 10); sh.text(190, y, money(ht, style), 10, right=True); y += 6
        sh.text(120, y, W["vat"], 10); sh.text(190, y, money(vat, style), 10, right=True); y += 6
        sh.text(120, y, W["total_incl"], 10, bold=True); sh.text(190, y, money(ttc, style), 10, bold=True, right=True); y += 6
        if deposit:
            dep_date = invoice_date - dt.timedelta(days=rng.randint(10, 40))
            sh.text(120, y, W["deposit"].format(d=fmt_date(dep_date, lang)), 10); sh.text(190, y, "– " + money(deposit, style), 10, right=True); y += 6
        sh.rule(110, 190, y - 2, 0.8)
        y += 2
        sh.text(120, y, W["to_pay"], 11, bold=True); sh.text(190, y, money(amount, style), 11, bold=True, right=True); y += 8
    else:
        sh.text(120, y, W["to_pay"], 11, bold=True); sh.text(190, y, money(amount, style), 11, bold=True, right=True); y += 5
        sh.text(190, y, W["vat_included"], 8, right=True); y += 8

    y = sh.paragraph(20, y, W["terms"].format(n=terms_days, d=fmt_date(due_date, lang)), 170, 10) + 2
    if no_qr:
        y = sh.paragraph(20, y, W["transfer"], 170, 10) + 1
        y = sh.lines(20, y, [f"{W['iban']} {iban_format(iban)}", f"{W['bank']} : {bank.name}, {bank.zip} {bank.town}", f"{W['holder']} : {creditor.name}"], 10, leading=4.6) + 2
    sh.paragraph(20, y, W["thanks"], 170, 10)
    if footer_date:
        other = invoice_date - dt.timedelta(days=rng.randint(60, 400))
        sh.text(20, 185, W["footer_terms"].format(d=fmt_date(other, lang)), 7)
        sh.text(190, 185, W["page"].format(p=1, n=1), 7, right=True)
    if not no_qr:
        draw_payment_part(sh, bill, lang, payload)
    sh.save()

    facts = {
        "issuer": creditor.name, "recipient": debtor.name, "amount_chf": amount, "currency": "CHF",
        "iban": iban, "reference": reference, "reference_type": reference_type, "invoice_number": invoice_no,
        "invoice_date": invoice_date.isoformat(), "due_date": due_date.isoformat(), "purpose": purpose,
    }
    return Truth(id=spec.pid, file=f"data/papers/{spec.pid}.pdf", kind="qr_bill", language=lang, facts=facts, traps=traps, pages=1)


def make_bank_statement(spec: Spec, rng: random.Random, out: str) -> Truth:
    lang, style = spec.lang, spec.style
    W = WORDS[lang]
    bank = rng.choice(BANKS[lang])
    holder = rng.choice(RECIPIENTS[lang])
    month = rng.choice([8, 9, 9, 10])
    period_start = dt.date(2026, month, 1)
    period_end = dt.date(2026, 10, 10) if month == 10 else (dt.date(2026, month + 1, 1) - dt.timedelta(days=1))
    iban = iban_build("CH", bank.clearing + f"{rng.randint(0, 999999999999):012d}")
    opening = rand_cents(rng, 800, 12000)

    # Mouvements : loyer, salaire, puis un tirage de petites opérations.
    moves: list[dict[str, Any]] = []
    moves.append({"date": (period_start + dt.timedelta(days=rng.randint(0, 2))).isoformat(),
                  "label": RENT_LABEL[lang].format(month=MONTHS[lang][month - 1]), "amount": -float(rng.randint(1150, 1950))})
    if period_end.day >= 25:
        moves.append({"date": (period_start + dt.timedelta(days=rng.randint(23, 26))).isoformat(),
                      "label": SALARY_LABEL[lang].format(employer=holder.employer), "amount": float(rng.randint(4200, 6900))})
    n = rng.randint(7, 15)
    for label, lo, hi, sign in rng.sample(MOVEMENTS[lang], min(n, len(MOVEMENTS[lang]))):
        moves.append({"date": rand_date(rng, period_start, period_end).isoformat(), "label": label, "amount": sign * rand_cents(rng, lo, hi)})
    moves.sort(key=lambda m: m["date"])
    computed_closing = round(opening + sum(m["amount"] for m in moves), 2)

    traps = [f"format_{style}"]
    balanced = "unbalanced" not in spec.flags
    if balanced:
        closing = computed_closing
    else:
        # Un solde final faux : un mouvement « oublié » dans la somme imprimée.
        closing = round(computed_closing + rng.choice([-1, 1]) * rand_cents(rng, 40, 900), 2)
        traps.append("unbalanced")
    footer_date = "footer_date" in spec.flags
    if footer_date:
        traps.append("footer_date")
    show_running = balanced and rng.random() < 0.5

    # --- dessin ---
    path = os.path.join(out, "papers", f"{spec.pid}.pdf")
    sh = Sheet(path, f"{W['statement']} {fmt_date(period_end, lang)}")
    cols = dict(date=20, label=42, debit=140, credit=165, balance=190)

    def header(first: bool) -> float:
        sh.text(20, 18, bank.name, 14, bold=True)
        sh.lines(20, 24, [bank.street, f"{bank.zip} {bank.town}"], 9)
        sh.rule(20, 190, 34, 0.6)
        if first:
            sh.lines(120, 42, [holder.name, f"{holder.street} {holder.no}", f"{holder.zip} {holder.town}"], 10)
            sh.text(20, 62, W["statement"], 14, bold=True)
            sh.lines(20, 70, [f"{W['account']}", f"{W['iban']} {iban_format(iban)}", f"{W['holder_label']} : {holder.name}",
                              W["period"].format(a=fmt_date(period_start, lang), b=fmt_date(period_end, lang))], 10, leading=4.8)
            y = 94
        else:
            y = 42
        sh.text(cols["date"], y, W["col_date"], 9, bold=True)
        sh.text(cols["label"], y, W["col_label"], 9, bold=True)
        sh.text(cols["debit"], y, W["col_debit"], 9, bold=True, right=True)
        sh.text(cols["credit"], y, W["col_credit"], 9, bold=True, right=True)
        if show_running:
            sh.text(cols["balance"], y, W["col_balance"], 9, bold=True, right=True)
        sh.rule(20, 190, y + 1.5, 0.4)
        return y + 7

    def footer(page: int, total: int) -> None:
        sh.text(20, 282, W["e_statement"], 7)
        if footer_date:
            sh.text(20, 286, W["printed"].format(d=fmt_date(period_end + dt.timedelta(days=rng.randint(2, 9)), lang)), 7)
        sh.text(190, 282, W["page"].format(p=page, n=total), 7, right=True)

    rows_per_page = 24
    total_pages = 1 + (len(moves) + 2 - 1) // rows_per_page
    y = header(True)
    page = 1
    running = opening
    sh.text(cols["date"], y, fmt_date(period_start, lang), 10)
    sh.text(cols["label"], y, W["opening"].format(d=fmt_date(period_start, lang)), 10, bold=True)
    sh.text(cols["balance"] if show_running else cols["credit"], y, signed_num(opening, style), 10, bold=True, right=True)
    y += 6
    count = 1
    for m in moves:
        if count >= rows_per_page:
            footer(page, total_pages)
            sh.new_page()
            page += 1
            y = header(False)
            count = 0
        running = round(running + m["amount"], 2)
        sh.text(cols["date"], y, fmt_date(dt.date.fromisoformat(m["date"]), lang), 10)
        sh.text(cols["label"], y, m["label"], 10)
        if m["amount"] < 0:
            sh.text(cols["debit"], y, num(m["amount"], style), 10, right=True)
        else:
            sh.text(cols["credit"], y, num(m["amount"], style), 10, right=True)
        if show_running:
            sh.text(cols["balance"], y, signed_num(running, style), 10, right=True)
        y += 6
        count += 1
    sh.rule(20, 190, y - 3.5, 0.6)
    sh.text(cols["date"], y, fmt_date(period_end, lang), 10)
    sh.text(cols["label"], y, W["closing"].format(d=fmt_date(period_end, lang)), 10, bold=True)
    sh.text(cols["balance"] if show_running else cols["credit"], y, signed_num(closing, style), 10, bold=True, right=True)
    y += 10
    sh.text(20, y, f"{W['col_debit']} : {money(-sum(m['amount'] for m in moves if m['amount'] < 0), style)}   "
                   f"{W['col_credit']} : {money(sum(m['amount'] for m in moves if m['amount'] > 0), style)}", 9)
    footer(page, total_pages)
    sh.save()

    facts = {
        "bank": bank.name, "account_iban": iban, "holder": holder.name,
        "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
        "opening_balance": opening, "closing_balance": closing, "movements": moves, "currency": "CHF",
    }
    return Truth(id=spec.pid, file=f"data/papers/{spec.pid}.pdf", kind="bank_statement", language=lang,
                 facts=facts, traps=traps, pages=sh.pages, balanced=balanced)


# Textes des lettres : (sujet, paragraphes, action) ; {amount} {deadline} {ref} {year} {fee} {debt} {costs} {premium}.
LETTER_TEXT: dict[str, dict[str, dict[str, Any]]] = {
    "fr": {
        "tax_decision": dict(
            subject="Décision de taxation {year} – impôt cantonal et communal",
            body=["Nous vous informons que votre taxation pour la période fiscale {year} a été établie sur la base de votre déclaration et des justificatifs remis.",
                  "Le montant de l'impôt cantonal et communal dû s'élève à {amount}. Les acomptes déjà versés ont été portés en déduction.",
                  "Nous vous prions de régler ce montant jusqu'au {deadline} au moyen du bulletin de versement joint, en indiquant la référence {ref}."],
            action="pay"),
        "payment_summons": dict(
            subject="Commandement de payer – poursuite n° {ref}",
            body=["À la requête du créancier mentionné ci-dessous, vous êtes sommé de payer la créance de {debt}, plus les frais de poursuite de {costs}, soit un total de {amount}.",
                  "Créancier : {creditor}.",
                  "Vous pouvez former opposition dans les dix jours dès la notification du présent acte auprès de l'office soussigné.",
                  "À défaut d'opposition ou de paiement intégral jusqu'au {deadline}, la poursuite suivra son cours."],
            action="pay"),
        "premium_notice": dict(
            subject="Primes {year} – assurance obligatoire des soins (LAMal)",
            body=["Nous vous communiquons votre nouvelle prime mensuelle pour l'assurance obligatoire des soins dès le 1er janvier {year}.",
                  "Prime mensuelle {year} : {amount} (franchise CHF 2'500, couverture accident incluse).",
                  "Si vous souhaitez changer d'assureur, votre résiliation doit nous parvenir au plus tard le {deadline}. Sans nouvelles de votre part, votre contrat se poursuit aux nouvelles conditions."],
            action="none"),
        "commune_notice": dict(
            subject="Contrôle des habitants – documents à fournir",
            body=["Suite à votre annonce d'arrivée dans notre commune, nous constatons que votre dossier est incomplet.",
                  "Nous vous remercions de nous faire parvenir une copie de votre contrat de bail et une attestation d'assurance-maladie jusqu'au {deadline}.",
                  "Ces documents peuvent être déposés au guichet ou envoyés par courrier postal. Aucun émolument n'est perçu pour cette démarche."],
            action="submit_documents"),
        "permit_decision": dict(
            subject="Permis de construire – décision (dossier {ref})",
            body=["La Municipalité a examiné votre demande de permis de construire concernant la transformation de votre habitation.",
                  "Le permis est délivré aux conditions figurant dans l'annexe. {fee_sentence}",
                  "La présente décision peut faire l'objet d'un recours jusqu'au {deadline} auprès de l'autorité compétente."],
            fee="L'émolument de décision s'élève à {amount} et doit être réglé dans les 30 jours.",
            no_fee="Aucun émolument n'est perçu, la demande entrant dans le cadre de la procédure simplifiée.",
            action="pay"),
    },
    "de": {
        "tax_decision": dict(
            subject="Veranlagungsverfügung {year} – Staats- und Gemeindesteuern",
            body=["Wir teilen Ihnen mit, dass Ihre Veranlagung für die Steuerperiode {year} aufgrund Ihrer Steuererklärung und der eingereichten Belege vorgenommen wurde.",
                  "Die geschuldeten Staats- und Gemeindesteuern betragen {amount}. Die bereits geleisteten Akontozahlungen wurden angerechnet.",
                  "Wir bitten Sie, den Betrag bis am {deadline} mit dem beiliegenden Einzahlungsschein zu begleichen. Bitte geben Sie die Referenz {ref} an."],
            action="pay"),
        "payment_summons": dict(
            subject="Zahlungsbefehl – Betreibung Nr. {ref}",
            body=["Auf Begehren des unten genannten Gläubigers werden Sie aufgefordert, die Forderung von {debt} zuzüglich Betreibungskosten von {costs}, total {amount}, zu bezahlen.",
                  "Gläubiger: {creditor}.",
                  "Sie können innert zehn Tagen seit Zustellung dieses Zahlungsbefehls beim unterzeichneten Amt Rechtsvorschlag erheben.",
                  "Ohne Rechtsvorschlag oder vollständige Zahlung bis am {deadline} wird die Betreibung fortgesetzt."],
            action="pay"),
        "premium_notice": dict(
            subject="Prämien {year} – obligatorische Krankenpflegeversicherung (KVG)",
            body=["Wir teilen Ihnen Ihre neue Monatsprämie für die obligatorische Krankenpflegeversicherung ab 1. Januar {year} mit.",
                  "Monatsprämie {year}: {amount} (Franchise CHF 2'500, mit Unfalldeckung).",
                  "Möchten Sie den Versicherer wechseln, muss Ihre Kündigung bis spätestens {deadline} bei uns eintreffen. Ohne Ihre Nachricht läuft der Vertrag zu den neuen Bedingungen weiter."],
            action="none"),
        "commune_notice": dict(
            subject="Einwohnerkontrolle – fehlende Unterlagen",
            body=["Nach Ihrer Anmeldung in unserer Gemeinde stellen wir fest, dass Ihr Dossier unvollständig ist.",
                  "Wir bitten Sie, uns bis am {deadline} eine Kopie Ihres Mietvertrags sowie einen Nachweis der Krankenversicherung zuzustellen.",
                  "Die Unterlagen können am Schalter abgegeben oder per Post gesandt werden. Für diesen Vorgang wird keine Gebühr erhoben."],
            action="submit_documents"),
        "permit_decision": dict(
            subject="Baubewilligung – Entscheid (Dossier {ref})",
            body=["Der Gemeinderat hat Ihr Baugesuch betreffend den Umbau Ihres Wohnhauses geprüft.",
                  "Die Bewilligung wird unter den im Anhang aufgeführten Auflagen erteilt. {fee_sentence}",
                  "Gegen diesen Entscheid kann bis am {deadline} bei der zuständigen Behörde Rekurs erhoben werden."],
            fee="Die Entscheidgebühr beträgt {amount} und ist innert 30 Tagen zu bezahlen.",
            no_fee="Es wird keine Gebühr erhoben, da das Gesuch im vereinfachten Verfahren behandelt wurde.",
            action="pay"),
    },
    "it": {
        "tax_decision": dict(
            subject="Decisione di tassazione {year} – imposta cantonale e comunale",
            body=["Vi informiamo che la vostra tassazione per il periodo fiscale {year} è stata allestita sulla base della dichiarazione e dei giustificativi consegnati.",
                  "L'imposta cantonale e comunale dovuta ammonta a {amount}. Gli acconti già versati sono stati dedotti.",
                  "Vi preghiamo di saldare l'importo entro il {deadline} con la polizza di versamento allegata, indicando il riferimento {ref}."],
            action="pay"),
        "payment_summons": dict(
            subject="Precetto esecutivo – esecuzione n. {ref}",
            body=["Su richiesta del creditore sotto indicato, siete intimati a pagare il credito di {debt}, oltre alle spese esecutive di {costs}, per un totale di {amount}.",
                  "Creditore: {creditor}.",
                  "Potete interporre opposizione entro dieci giorni dalla notifica del presente atto presso l'ufficio sottoscritto.",
                  "In assenza di opposizione o di pagamento integrale entro il {deadline}, l'esecuzione proseguirà."],
            action="pay"),
        "premium_notice": dict(
            subject="Premi {year} – assicurazione obbligatoria delle cure (LAMal)",
            body=["Vi comunichiamo il vostro nuovo premio mensile per l'assicurazione obbligatoria delle cure a partire dal 1° gennaio {year}.",
                  "Premio mensile {year}: {amount} (franchigia CHF 2'500, copertura infortuni inclusa).",
                  "Se desiderate cambiare assicuratore, la disdetta deve pervenirci entro il {deadline}. Senza vostre notizie, il contratto prosegue alle nuove condizioni."],
            action="none"),
        "commune_notice": dict(
            subject="Controllo abitanti – documenti mancanti",
            body=["In seguito alla vostra notifica di arrivo nel nostro Comune, constatiamo che il vostro incarto è incompleto.",
                  "Vi preghiamo di farci pervenire entro il {deadline} una copia del contratto di locazione e un attestato di assicurazione malattia.",
                  "I documenti possono essere consegnati allo sportello o inviati per posta. Per questa pratica non viene riscossa alcuna tassa."],
            action="submit_documents"),
        "permit_decision": dict(
            subject="Licenza edilizia – decisione (incarto {ref})",
            body=["Il Municipio ha esaminato la vostra domanda di licenza edilizia relativa alla trasformazione della vostra abitazione.",
                  "La licenza è rilasciata alle condizioni indicate nell'allegato. {fee_sentence}",
                  "Contro la presente decisione è dato ricorso entro il {deadline} all'autorità competente."],
            fee="La tassa di decisione ammonta a {amount} ed è da pagare entro 30 giorni.",
            no_fee="Non viene riscossa alcuna tassa, poiché la domanda rientra nella procedura semplificata.",
            action="pay"),
    },
}


def make_official_letter(spec: Spec, rng: random.Random, out: str, letter_kind: str) -> Truth:
    lang, style = spec.lang, spec.style
    W = WORDS[lang]
    T = LETTER_TEXT[lang][letter_kind]
    authority, street, zip_town, canton, code = rng.choice(AUTHORITIES[lang][letter_kind])
    recipient = rng.choice(RECIPIENTS[lang])
    letter_date = rand_date(rng, dt.date(2026, 8, 3), dt.date(2026, 10, 12))
    ref = f"{letter_date.year}-{rng.randint(10000, 99999)}-{code}"
    traps = [f"format_{style}"]
    year = 2025
    amount: float | None = None
    action = T["action"]
    extra: dict[str, str] = {}

    if letter_kind == "tax_decision":
        amount = rand_cents(rng, 1200, 9800)
        deadline = letter_date + dt.timedelta(days=30)
    elif letter_kind == "payment_summons":
        debt = rand_cents(rng, 300, 4500)
        costs = rng.choice([53.30, 73.30, 90.30, 103.30])
        amount = round(debt + costs, 2)
        deadline = letter_date + dt.timedelta(days=20)
        extra = dict(debt=money(debt, style), costs=money(costs, style), creditor=rng.choice(CREDITORS[lang]).name)
        traps.append("second_amount")
    elif letter_kind == "premium_notice":
        amount = rand_cents(rng, 280, 620)
        year = 2027
        deadline = dt.date(2026, 11, 30)
    elif letter_kind == "commune_notice":
        amount = None
        deadline = letter_date + dt.timedelta(days=rng.choice([14, 20, 30]))
        traps.append("no_amount")
    else:  # permit_decision
        deadline = letter_date + dt.timedelta(days=30)
        if "no_amount" in spec.flags:
            amount = None
            action = "none"
            traps.append("no_amount")
        else:
            amount = float(rng.choice([150, 250, 450, 680, 900, 1200]))
    footer_date = "footer_date" in spec.flags
    if footer_date:
        traps.append("footer_date")

    fmt = dict(year=year, ref=ref, deadline=fmt_date(deadline, lang), amount=money(amount, style) if amount is not None else "", **extra)
    if letter_kind == "permit_decision":
        fmt["fee_sentence"] = (T["fee"] if amount is not None else T["no_fee"]).format(**fmt)
    subject = T["subject"].format(**fmt)
    paragraphs = [p.format(**fmt) for p in T["body"]]

    # --- dessin ---
    path = os.path.join(out, "papers", f"{spec.pid}.pdf")
    sh = Sheet(path, subject)
    sh.text(20, 18, canton, 9)
    sh.text(20, 24, authority, 13, bold=True)
    sh.lines(20, 30, [street, zip_town, W["phone"]], 9)
    sh.rule(20, 190, 42, 0.6)
    sh.lines(120, 50, [recipient.name, f"{recipient.street} {recipient.no}", f"{recipient.zip} {recipient.town}"], 10)
    town = zip_town.split(" ", 1)[1]
    sh.text(20, 72, date_line(town, letter_date, lang), 10)
    sh.text(20, 80, f"{W['ref']} {ref}", 9)
    sh.text(20, 92, subject, 12, bold=True)
    y = 102
    y = sh.paragraph(20, y, W["greeting"], 170, 10) + 3
    for p in paragraphs:
        y = sh.paragraph(20, y, p, 170, 10) + 3
    y = sh.paragraph(20, y + 2, W["closing_formula"], 170, 10) + 6
    sh.text(20, y, authority, 10, bold=True)
    if letter_kind in ("tax_decision", "permit_decision", "payment_summons"):
        sh.paragraph(20, 250, W["appeal"], 170, 8)
    if footer_date:
        other = letter_date - dt.timedelta(days=rng.randint(90, 600))
        sh.text(20, 282, W["form_version"].format(d=fmt_date(other, lang)), 7)
    sh.text(190, 282, W["page"].format(p=1, n=1), 7, right=True)
    sh.save()

    facts = {
        "authority": authority, "recipient": recipient.name, "letter_date": letter_date.isoformat(), "subject": subject,
        "letter_kind": letter_kind, "amount_chf": amount, "deadline": deadline.isoformat(), "reference_number": ref,
        "action_required": action,
    }
    return Truth(id=spec.pid, file=f"data/papers/{spec.pid}.pdf", kind="official_letter", language=lang, facts=facts, traps=traps, pages=1)


# =============================================================================
# Plan, questions, point d'entrée
# =============================================================================


def build_specs(rng: random.Random, plan: dict[tuple[str, str], int]) -> list[Spec]:
    """Décide genre, langue, style et pièges de chaque papier, puis mélange et numérote."""
    specs: list[Spec] = []
    for (kind, lang), n in plan.items():
        for _ in range(n):
            specs.append(Spec(pid="", kind=kind, lang=lang, style=rng.choice(STYLES)))
    bills = [s for s in specs if s.kind == "qr_bill"]
    statements = [s for s in specs if s.kind == "bank_statement"]
    letters = [s for s in specs if s.kind == "official_letter"]
    for s in rng.sample(bills, min(BILLS_WITHOUT_QR, len(bills))):
        s.flags.add("no_qr")
    for s in bills:
        if rng.random() < 0.45:
            s.flags.add("second_amount")
        if rng.random() < 0.4:
            s.flags.add("footer_date")
        if "no_qr" not in s.flags and rng.random() < 0.35:
            s.flags.add("creditor_name_drift")
    for s in rng.sample(statements, min(UNBALANCED_STATEMENTS, len(statements))):
        s.flags.add("unbalanced")
    for s in statements:
        if rng.random() < 0.4:
            s.flags.add("footer_date")
    # Genres de lettres en rotation pour couvrir les cinq, et un permis sur deux sans émolument.
    kinds = ["tax_decision", "payment_summons", "premium_notice", "commune_notice", "permit_decision"]
    for i, s in enumerate(letters):
        s.flags.add("letter:" + kinds[i % len(kinds)])
        if kinds[i % len(kinds)] == "permit_decision" and (i // len(kinds)) % 2 == 1:
            s.flags.add("no_amount")
        if rng.random() < 0.4:
            s.flags.add("footer_date")
    rng.shuffle(specs)
    for i, s in enumerate(specs, start=1):
        s.pid = f"p{i:03d}"
    return specs


def generate(out: str, seed: int = 42, plan: dict[tuple[str, str], int] | None = None) -> list[Truth]:
    """Génère tous les papiers et leur vérité dans ``out`` ; rend les vérités."""
    rng = random.Random(seed)
    plan = plan or DEFAULT_PLAN
    os.makedirs(os.path.join(out, "papers"), exist_ok=True)
    os.makedirs(os.path.join(out, "truth"), exist_ok=True)
    truths: list[Truth] = []
    for spec in build_specs(rng, plan):
        if spec.kind == "qr_bill":
            t = make_qr_bill(spec, rng, out)
        elif spec.kind == "bank_statement":
            t = make_bank_statement(spec, rng, out)
        else:
            letter_kind = next(f.split(":", 1)[1] for f in spec.flags if f.startswith("letter:"))
            t = make_official_letter(spec, rng, out, letter_kind)
        with open(os.path.join(out, "truth", f"{spec.pid}.json"), "w", encoding="utf-8") as fh:
            fh.write(t.to_json() + "\n")
        truths.append(t)
    questions = build_questions(truths, rng)
    with open(os.path.join(out, "questions.json"), "w", encoding="utf-8") as fh:
        json.dump(questions, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return truths


Q_TEXT = {
    "owe": {"fr": "Combien dois-je encore à {issuer} ?", "de": "Wie viel schulde ich {issuer} noch?", "it": "Quanto devo ancora a {issuer}?"},
    "owe_no": {"fr": "Combien dois-je encore à {issuer} pour la facture {no} ?", "de": "Wie viel schulde ich {issuer} noch für die Rechnung {no}?", "it": "Quanto devo ancora a {issuer} per la fattura {no}?"},
    "balance": {"fr": "Quel est le solde du compte chez {bank} au {date} ?", "de": "Wie hoch ist der Saldo des Kontos bei {bank} am {date}?", "it": "Qual è il saldo del conto presso {bank} al {date}?"},
    "account_tail": {"fr": "{bank} (compte se terminant par {tail})", "de": "{bank} (Konto endend auf {tail})", "it": "{bank} (conto che termina con {tail})"},
    "due_on": {"fr": "Quelles factures sont dues le {date} ?", "de": "Welche Rechnungen sind am {date} fällig?", "it": "Quali fatture scadono il {date}?"},
    "count_month": {"fr": "Combien de factures ai-je reçues en {month} ?", "de": "Wie viele Rechnungen habe ich im {month} erhalten?", "it": "Quante fatture ho ricevuto in {month}?"},
    "deadline": {"fr": "Quel est le délai de {authority} (réf. {ref}) ?", "de": "Welche Frist setzt {authority} (Ref. {ref})?", "it": "Qual è il termine fissato da {authority} (rif. {ref})?"},
    "total_month": {"fr": "Total des factures reçues en {month} ?", "de": "Total der im {month} erhaltenen Rechnungen?", "it": "Totale delle fatture ricevute in {month}?"},
}


def build_questions(truths: list[Truth], rng: random.Random, target: int = 50) -> list[dict[str, Any]]:
    """50 questions en fr/de/it, chacune avec sa vérité et les papiers qui y répondent."""
    bills = [t for t in truths if t.kind == "qr_bill"]
    statements = [t for t in truths if t.kind == "bank_statement" and t.balanced]
    letters = [t for t in truths if t.kind == "official_letter"]
    qs: list[dict[str, Any]] = []
    langs_cycle = ["fr", "de", "fr", "de", "it"]

    def add(kind: str, lang: str, text: str, qtype: str, truth: Any, papers: list[str]) -> None:
        qs.append({"id": f"q{len(qs) + 1:02d}", "lang": lang, "kind": kind, "question": text, "type": qtype, "truth": truth, "papers": papers})

    # 1. Combien dois-je encore à … (12)
    issuer_count: dict[str, int] = {}
    for b in bills:
        issuer_count[b.facts["issuer"]] = issuer_count.get(b.facts["issuer"], 0) + 1
    for b in rng.sample(bills, min(12, len(bills))):
        f = b.facts
        if issuer_count[f["issuer"]] == 1:
            text = Q_TEXT["owe"][b.language].format(issuer=f["issuer"])
        else:
            text = Q_TEXT["owe_no"][b.language].format(issuer=f["issuer"], no=f["invoice_number"])
        add("owe", b.language, text, "amount", f["amount_chf"], [b.id])

    # 2. Solde du compte au … (10) — relevés équilibrés seulement ; si plusieurs relevés (même les faux)
    #    partagent (banque, fin de période), la question nomme le compte par ses 4 derniers chiffres.
    all_statements = [t for t in truths if t.kind == "bank_statement"]
    key_count: dict[tuple[str, str], int] = {}
    for t in all_statements:
        k = (t.facts["bank"], t.facts["period_end"])
        key_count[k] = key_count.get(k, 0) + 1
    for s in rng.sample(statements, len(statements)):
        d = dt.date.fromisoformat(s.facts["period_end"])
        bank = s.facts["bank"]
        if key_count[(bank, s.facts["period_end"])] > 1:
            bank = Q_TEXT["account_tail"][s.language].format(bank=bank, tail=s.facts["account_iban"][-4:])
        add("balance", s.language, Q_TEXT["balance"][s.language].format(bank=bank, date=fmt_date(d, s.language)),
            "amount", s.facts["closing_balance"], [s.id])
        if sum(1 for q in qs if q["kind"] == "balance") >= 10:
            break

    # 3. Quelles factures sont dues le … (8) — des dates avec au moins une facture, plusieurs si possible
    by_due: dict[str, list[str]] = {}
    for b in bills:
        by_due.setdefault(b.facts["due_date"], []).append(b.id)
    dates = sorted(by_due, key=lambda d: (-len(by_due[d]), d))[:8]
    for i, d in enumerate(dates):
        lang = langs_cycle[i % len(langs_cycle)]
        add("due_on", lang, Q_TEXT["due_on"][lang].format(date=fmt_date(dt.date.fromisoformat(d), lang)), "list", sorted(by_due[d]), sorted(by_due[d]))

    # 4. Combien de factures en … (6) et 6. total des factures en … (6)
    for i, (lang, month) in enumerate([("fr", 8), ("de", 8), ("it", 9), ("fr", 9), ("de", 9), ("it", 8)]):
        ids = sorted(b.id for b in bills if dt.date.fromisoformat(b.facts["invoice_date"]).month == month)
        mname = MONTHS[lang][month - 1]
        add("count_month", lang, Q_TEXT["count_month"][lang].format(month=mname), "count", len(ids), ids)
    for i, (lang, month) in enumerate([("fr", 9), ("de", 9), ("it", 9), ("fr", 8), ("de", 8), ("it", 10)]):
        sel = [b for b in bills if dt.date.fromisoformat(b.facts["invoice_date"]).month == month]
        mname = MONTHS[lang][month - 1]
        add("total_month", lang, Q_TEXT["total_month"][lang].format(month=mname), "amount",
            round(sum(b.facts["amount_chf"] for b in sel), 2), sorted(b.id for b in sel))

    # 5. Quel est le délai de … (8)
    for lt in rng.sample(letters, min(8, len(letters))):
        f = lt.facts
        add("deadline", lt.language, Q_TEXT["deadline"][lt.language].format(authority=f["authority"], ref=f["reference_number"]),
            "date", f["deadline"], [lt.id])

    # Complément jusqu'à 50 avec d'autres « combien dois-je » si besoin.
    used = {q["papers"][0] for q in qs if q["kind"] == "owe"}
    for b in bills:
        if len(qs) >= target:
            break
        if b.id in used:
            continue
        f = b.facts
        add("owe", b.language, Q_TEXT["owe_no"][b.language].format(issuer=f["issuer"], no=f["invoice_number"]), "amount", f["amount_chf"], [b.id])
    return qs[:target]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Génère des papiers suisses synthétiques (PDF + vérité JSON).")
    ap.add_argument("--out", default="data", help="dossier de sortie (papers/, truth/, questions.json)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    truths = generate(args.out, args.seed)
    kinds: dict[str, int] = {}
    langs: dict[str, int] = {}
    for t in truths:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
        langs[t.language] = langs.get(t.language, 0) + 1
    print(f"{len(truths)} papiers écrits dans {args.out} : {kinds} ; langues {langs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
