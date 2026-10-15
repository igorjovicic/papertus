"""Chaînes de l'interface en quatre langues (fr par défaut, de, it, en).

Une seule table, une seule fonction `t(cle, lang)`. L'allemand suit l'orthographe
suisse (jamais de ß). Les raisons de refus produites par le code restent en anglais
(niveau code, voir schema.Field.reason) ; l'interface les affiche telles quelles.
"""

from __future__ import annotations

LANGS: tuple[str, ...] = ("fr", "de", "it", "en")
DEFAULT_LANG = "fr"

# Le nom affiché partout (page, titres, rapport, README). Le paquet Python garde son
# nom de module pour l'instant ; seul ce libellé compte pour ce que voit la personne.
APP_NAME = "Papertus"

LANG_NAMES = {"fr": "Français", "de": "Deutsch", "it": "Italiano", "en": "English"}

# Clé -> (fr, de, it, en). L'ordre des langues est celui de LANGS.
_S: dict[str, tuple[str, str, str, str]] = {
    # --- Général -------------------------------------------------------------
    "app.title": (APP_NAME, APP_NAME, APP_NAME, APP_NAME),
    "app.tagline": (
        "Parlez à vos papiers, sur votre ordinateur. Rien ne sort.",
        "Sprechen Sie mit Ihren Papieren, auf Ihrem Computer. Nichts verlässt ihn.",
        "Parlate con i vostri documenti, sul vostro computer. Nulla esce.",
        "Talk to your papers, on your own computer. Nothing leaves it.",
    ),
    "status.local": (
        "Apertus {size} · sur cette machine · rien ne sort",
        "Apertus {size} · auf diesem Gerät · nichts geht raus",
        "Apertus {size} · su questo computer · nulla esce",
        "Apertus {size} · on this machine · nothing leaves",
    ),
    "status.cloud": (
        "Apertus {size} · cloud suisse",
        "Apertus {size} · Schweizer Cloud",
        "Apertus {size} · cloud svizzero",
        "Apertus {size} · Swiss cloud",
    ),
    "status.none": (
        "Aucun modèle configuré",
        "Kein Modell eingerichtet",
        "Nessun modello configurato",
        "No model configured",
    ),
    "apertus.label": (
        "Apertus {size} · chaque chiffre contrôlé",
        "Apertus {size} · jede Zahl geprüft",
        "Apertus {size} · ogni cifra controllata",
        "Apertus {size} · every figure checked",
    ),
    "empty.line": (
        "Vos factures, relevés et lettres officielles, lus ici, sur votre ordinateur. Déposez un dossier pour commencer.",
        "Ihre Rechnungen, Kontoauszüge und amtlichen Briefe, hier gelesen, auf Ihrem Computer. Legen Sie einen Ordner ab, um zu beginnen.",
        "Le vostre fatture, estratti conto e lettere ufficiali, letti qui, sul vostro computer. Trascinate una cartella per iniziare.",
        "Your bills, bank statements and official letters, read here, on your own computer. Drop a folder to begin.",
    ),
    "tab.papers": ("Papiers", "Papiere", "Documenti", "Papers"),
    "tab.report": ("Rapport", "Bericht", "Rapporto", "Report"),
    "tab.ask": ("Question", "Frage", "Domanda", "Ask"),
    "lang.label": ("Langue", "Sprache", "Lingua", "Language"),
    "month.label": ("Mois", "Monat", "Mese", "Month"),
    "loading": ("Chargement…", "Wird geladen…", "Caricamento…", "Loading…"),
    "error": ("Erreur", "Fehler", "Errore", "Error"),
    "none": ("—", "—", "—", "—"),
    "chf": ("CHF", "CHF", "CHF", "CHF"),

    # --- Dépôt ---------------------------------------------------------------
    "drop.title": (
        "Déposez vos factures QR, relevés, lettres du canton, de la commune, de la caisse-maladie",
        "Legen Sie Ihre QR-Rechnungen, Kontoauszüge, Briefe von Kanton, Gemeinde und Krankenkasse ab",
        "Trascinate le vostre fatture QR, estratti conto, lettere del Cantone, del Comune, della cassa malati",
        "Drop your QR-bills, bank statements, letters from the canton, the commune, the health insurer",
    ),
    "drop.hint": (
        "Un dossier entier ou plusieurs PDF, glissés ici.",
        "Ein ganzer Ordner oder mehrere PDF, hierher gezogen.",
        "Un'intera cartella o più PDF, trascinati qui.",
        "A whole folder or several PDFs, dragged here.",
    ),
    "drop.folder": ("Choisir un dossier", "Ordner wählen", "Scegli una cartella", "Choose a folder"),
    "drop.files": ("Choisir des fichiers", "Dateien wählen", "Scegli dei file", "Choose files"),
    "drop.reading": ("Lecture", "Lesen", "Lettura", "Reading"),
    "drop.done": ("Terminé", "Fertig", "Fatto", "Done"),
    "drop.uploaded": ("fichiers reçus", "Dateien erhalten", "file ricevuti", "files received"),
    "drop.nothing": ("Aucun PDF dans l'envoi.", "Kein PDF in der Sendung.", "Nessun PDF nell'invio.", "No PDF in the upload."),

    # --- Liste et fiche ------------------------------------------------------
    "papers.empty": (
        "Aucun papier pour l'instant. Déposez un dossier ci-dessus.",
        "Noch keine Papiere. Legen Sie oben einen Ordner ab.",
        "Nessun documento per ora. Trascinate una cartella qui sopra.",
        "No papers yet. Drop a folder above.",
    ),
    "papers.count": ("papiers", "Papiere", "documenti", "papers"),
    "paper.open": ("Ouvrir le PDF", "PDF öffnen", "Apri il PDF", "Open the PDF"),
    "paper.pages": ("pages", "Seiten", "pagine", "pages"),
    "paper.text": ("Texte extrait", "Extrahierter Text", "Testo estratto", "Extracted text"),
    "paper.qr": ("Code QR lu", "QR-Code gelesen", "Codice QR letto", "QR code read"),
    "paper.select": (
        "Choisissez un papier dans la liste.",
        "Wählen Sie ein Papier in der Liste.",
        "Scegliete un documento nell'elenco.",
        "Pick a paper from the list.",
    ),
    "status.verified": ("vérifié par le code", "vom Code geprüft", "verificato dal codice", "checked by code"),
    "status.model": ("modèle seul, non vérifié", "nur Modell, ungeprüft", "solo modello, non verificato", "model only, unchecked"),
    "status.blank": ("laissé vide", "leer gelassen", "lasciato vuoto", "left blank"),
    "status.reason": ("raison", "Grund", "motivo", "reason"),
    "legend.title": ("Légende", "Legende", "Legenda", "Legend"),

    # --- Genres de papier ----------------------------------------------------
    "kind.qr_bill": ("Facture QR", "QR-Rechnung", "Fattura QR", "QR-bill"),
    "kind.bank_statement": ("Relevé bancaire", "Kontoauszug", "Estratto conto", "Bank statement"),
    "kind.official_letter": ("Lettre officielle", "Amtlicher Brief", "Lettera ufficiale", "Official letter"),
    "kind.unknown": ("Inconnu", "Unbekannt", "Sconosciuto", "Unknown"),

    # --- Champs --------------------------------------------------------------
    "field.issuer": ("Émetteur", "Aussteller", "Emittente", "Issuer"),
    "field.recipient": ("Destinataire", "Empfänger", "Destinatario", "Recipient"),
    "field.amount_chf": ("Montant", "Betrag", "Importo", "Amount"),
    "field.currency": ("Monnaie", "Währung", "Valuta", "Currency"),
    "field.iban": ("IBAN", "IBAN", "IBAN", "IBAN"),
    "field.reference": ("Référence", "Referenz", "Riferimento", "Reference"),
    "field.reference_type": ("Type de référence", "Referenztyp", "Tipo di riferimento", "Reference type"),
    "field.invoice_number": ("N° de facture", "Rechnungsnummer", "N. fattura", "Invoice number"),
    "field.invoice_date": ("Date de facture", "Rechnungsdatum", "Data fattura", "Invoice date"),
    "field.due_date": ("Échéance", "Fälligkeit", "Scadenza", "Due date"),
    "field.purpose": ("Objet", "Zweck", "Oggetto", "Purpose"),
    "field.bank": ("Banque", "Bank", "Banca", "Bank"),
    "field.account_iban": ("Compte (IBAN)", "Konto (IBAN)", "Conto (IBAN)", "Account (IBAN)"),
    "field.holder": ("Titulaire", "Inhaber", "Titolare", "Holder"),
    "field.period_start": ("Début de période", "Periodenbeginn", "Inizio periodo", "Period start"),
    "field.period_end": ("Fin de période", "Periodenende", "Fine periodo", "Period end"),
    "field.opening_balance": ("Solde d'ouverture", "Anfangssaldo", "Saldo iniziale", "Opening balance"),
    "field.closing_balance": ("Solde de clôture", "Schlusssaldo", "Saldo finale", "Closing balance"),
    "field.movements": ("Mouvements", "Bewegungen", "Movimenti", "Movements"),
    "field.authority": ("Autorité", "Behörde", "Autorità", "Authority"),
    "field.letter_date": ("Date de la lettre", "Briefdatum", "Data della lettera", "Letter date"),
    "field.subject": ("Sujet", "Betreff", "Oggetto", "Subject"),
    "field.letter_kind": ("Type de lettre", "Briefart", "Tipo di lettera", "Letter kind"),
    "field.deadline": ("Délai", "Frist", "Termine", "Deadline"),
    "field.reference_number": ("N° de référence", "Referenznummer", "N. di riferimento", "Reference number"),
    "field.action_required": ("Action requise", "Handlung nötig", "Azione richiesta", "Action required"),

    # --- Rapport -------------------------------------------------------------
    "report.title": ("Rapport du mois", "Monatsbericht", "Rapporto del mese", "Monthly report"),
    "report.bills": ("Factures reçues", "Erhaltene Rechnungen", "Fatture ricevute", "Bills received"),
    "report.accounts": ("Comptes bancaires", "Bankkonten", "Conti bancari", "Bank accounts"),
    "report.letters": ("Lettres demandant une action", "Briefe, die eine Handlung verlangen", "Lettere che richiedono un'azione", "Letters requiring action"),
    "report.summary": ("En quelques mots", "In wenigen Worten", "In poche parole", "In a few words"),
    "report.col.issuer": ("Émetteur", "Aussteller", "Emittente", "Issuer"),
    "report.col.invoice_date": ("Date", "Datum", "Data", "Date"),
    "report.col.due_date": ("Échéance", "Fälligkeit", "Scadenza", "Due"),
    "report.col.amount": ("Montant CHF", "Betrag CHF", "Importo CHF", "Amount CHF"),
    "report.col.state": ("État", "Stand", "Stato", "State"),
    "report.col.account": ("Compte", "Konto", "Conto", "Account"),
    "report.col.opening": ("Ouverture", "Anfang", "Apertura", "Opening"),
    "report.col.in": ("Entrées", "Eingänge", "Entrate", "In"),
    "report.col.out": ("Sorties", "Ausgänge", "Uscite", "Out"),
    "report.col.closing": ("Clôture", "Schluss", "Chiusura", "Closing"),
    "report.col.authority": ("Autorité", "Behörde", "Autorità", "Authority"),
    "report.col.subject": ("Sujet", "Betreff", "Oggetto", "Subject"),
    "report.col.deadline": ("Délai", "Frist", "Termine", "Deadline"),
    "report.col.paper": ("Papier", "Papier", "Documento", "Paper"),
    "report.state.to_pay": ("à payer", "zu zahlen", "da pagare", "to pay"),
    "report.state.unknown": ("paiement inconnu", "Zahlung unbekannt", "pagamento sconosciuto", "payment unknown"),
    "report.total": ("Total", "Total", "Totale", "Total"),
    "report.total_to_pay": ("Total à payer", "Total zu zahlen", "Totale da pagare", "Total to pay"),
    "report.unbalanced": (
        "Attention : les mouvements de ce relevé ne tombent pas sur le solde de clôture.",
        "Achtung: Die Bewegungen dieses Auszugs ergeben nicht den Schlusssaldo.",
        "Attenzione: i movimenti di questo estratto non corrispondono al saldo finale.",
        "Warning: the movements of this statement do not add up to the closing balance.",
    ),
    "report.empty": (
        "Aucun papier pour ce mois.",
        "Keine Papiere für diesen Monat.",
        "Nessun documento per questo mese.",
        "No paper for this month.",
    ),
    "report.sentence_removed": (
        "(une phrase retirée : chiffre non trouvé dans les papiers)",
        "(ein Satz entfernt: Zahl nicht in den Papieren gefunden)",
        "(una frase rimossa: cifra non trovata nei documenti)",
        "(one sentence removed: figure not found in the papers)",
    ),
    "report.no_prose": (
        "(pas de résumé : le modèle n'a pas répondu)",
        "(keine Zusammenfassung: das Modell hat nicht geantwortet)",
        "(nessun riassunto: il modello non ha risposto)",
        "(no summary: the model did not answer)",
    ),
    "report.generate": ("Générer le rapport", "Bericht erstellen", "Genera il rapporto", "Generate the report"),
    "report.markdown": ("Télécharger en Markdown", "Als Markdown herunterladen", "Scarica in Markdown", "Download as Markdown"),

    # --- Question ------------------------------------------------------------
    "ask.placeholder": (
        "Posez une question sur vos papiers…",
        "Stellen Sie eine Frage zu Ihren Papieren…",
        "Fate una domanda sui vostri documenti…",
        "Ask a question about your papers…",
    ),
    "ask.button": ("Demander", "Fragen", "Chiedi", "Ask"),
    "ask.no_paper": (
        "Aucun papier ne répond à cette question.",
        "Kein Papier beantwortet diese Frage.",
        "Nessun documento risponde a questa domanda.",
        "No paper answers this question.",
    ),
    "ask.not_found": (
        "Je ne trouve pas ce chiffre dans vos papiers.",
        "Ich finde diese Zahl nicht in Ihren Papieren.",
        "Non trovo questa cifra nei vostri documenti.",
        "I can't find that figure in your papers.",
    ),
    "ask.sources": ("Sources", "Quellen", "Fonti", "Sources"),
    "ask.examples": ("Par exemple", "Zum Beispiel", "Per esempio", "For example"),
    "ask.chip.1": (
        "Combien dois-je encore à la caisse-maladie ?",
        "Was schulde ich der Krankenkasse noch?",
        "Quanto devo ancora alla cassa malati?",
        "How much do I still owe the health insurer?",
    ),
    "ask.chip.2": (
        "Quel est le délai de l'Office des poursuites ?",
        "Welche Frist setzt das Betreibungsamt?",
        "Qual è il termine dell'Ufficio esecuzioni?",
        "What is the deadline from the debt collection office?",
    ),
    "ask.chip.3": (
        "Combien dois-je encore à la commune ?",
        "Was schulde ich der Gemeinde noch?",
        "Quanto devo ancora al Comune?",
        "How much do I still owe the commune?",
    ),
    "ask.chip.4": (
        "Quel est le délai de l'Office des impôts ?",
        "Welche Frist setzt das Steueramt?",
        "Quanto devo ancora all'Ufficio delle imposte?",
        "What is the deadline from the tax office?",
    ),
    "ask.chip.5": (
        "Total des factures de septembre ?",
        "Total der Rechnungen im September?",
        "Totale delle fatture di settembre?",
        "Total of September's bills?",
    ),
    "ask.grounded": ("chiffres vérifiés", "Zahlen geprüft", "cifre verificate", "figures checked"),
    "ask.not_grounded": ("non vérifié", "ungeprüft", "non verificato", "unchecked"),
    "ask.by_code": ("calculé par le code", "vom Code berechnet", "calcolato dal codice", "computed by code"),
    "ask.by_model": ("rédigé par le modèle", "vom Modell geschrieben", "scritto dal modello", "written by the model"),
    "ask.note.model_silent": (
        "Le modèle n'a pas répondu.",
        "Das Modell hat nicht geantwortet.",
        "Il modello non ha risposto.",
        "The model did not answer.",
    ),
    "ask.note.no_citation": (
        "La réponse ne citait aucun papier.",
        "Die Antwort nannte kein Papier.",
        "La risposta non citava alcun documento.",
        "The answer cited no paper.",
    ),
    "ask.note.ungrounded": (
        "Un chiffre de la réponse n'est pas dans les papiers cités.",
        "Eine Zahl der Antwort steht nicht in den zitierten Papieren.",
        "Una cifra della risposta non è nei documenti citati.",
        "A figure in the answer is not in the cited papers.",
    ),
    "ask.answer.sum_due": (
        "Factures échues jusqu'au {date} : {n} papier(s), total CHF {total}.",
        "Bis am {date} fällige Rechnungen: {n} Papier(e), total CHF {total}.",
        "Fatture in scadenza entro il {date}: {n} documento/i, totale CHF {total}.",
        "Bills due by {date}: {n} paper(s), total CHF {total}.",
    ),
    "ask.answer.sum_month": (
        "Factures reçues en {month} : {n}, total CHF {total}.",
        "Im {month} erhaltene Rechnungen: {n}, total CHF {total}.",
        "Fatture ricevute in {month}: {n}, totale CHF {total}.",
        "Bills received in {month}: {n}, total CHF {total}.",
    ),
    "ask.answer.count_month": (
        "{n} {what} en {month}.",
        "{n} {what} im {month}.",
        "{n} {what} in {month}.",
        "{n} {what} in {month}.",
    ),
    "ask.answer.balance": (
        "Solde du compte {account} au {date} : CHF {amount}.",
        "Saldo des Kontos {account} am {date}: CHF {amount}.",
        "Saldo del conto {account} al {date}: CHF {amount}.",
        "Balance of account {account} on {date}: CHF {amount}.",
    ),
    "ask.answer.balance_blank": (
        "Le solde de clôture du relevé {account} au {date} a été laissé vide : {reason}.",
        "Der Schlusssaldo des Auszugs {account} am {date} wurde leer gelassen: {reason}.",
        "Il saldo finale dell'estratto {account} al {date} è stato lasciato vuoto: {reason}.",
        "The closing balance of statement {account} on {date} was left blank: {reason}.",
    ),
    "ask.answer.owed": (
        "Dû à {issuer} : CHF {total} ({n} facture(s)).",
        "Geschuldet an {issuer}: CHF {total} ({n} Rechnung(en)).",
        "Dovuto a {issuer}: CHF {total} ({n} fattura/e).",
        "Owed to {issuer}: CHF {total} ({n} bill(s)).",
    ),
    "ask.answer.deadline": (
        "Délai de {authority} : {date}.",
        "Frist von {authority}: {date}.",
        "Termine di {authority}: {date}.",
        "Deadline from {authority}: {date}.",
    ),
    "ask.answer.due_on": (
        "Factures échues le {date} : {ids}.",
        "Am {date} fällige Rechnungen: {ids}.",
        "Fatture in scadenza il {date}: {ids}.",
        "Bills due on {date}: {ids}.",
    ),
    "ask.answer.none_due": (
        "Aucune facture n'est échue le {date}.",
        "Am {date} ist keine Rechnung fällig.",
        "Nessuna fattura scade il {date}.",
        "No bill is due on {date}.",
    ),

    # --- Mots comptés (pour les réponses de dénombrement) --------------------
    "what.bills": ("facture(s) reçue(s)", "Rechnung(en) erhalten", "fattura/e ricevuta/e", "bill(s) received"),
    "what.letters": ("lettre(s) reçue(s)", "Brief(e) erhalten", "lettera/e ricevuta/e", "letter(s) received"),
    "what.statements": ("relevé(s) bancaire(s)", "Kontoauszug/-züge", "estratto/i conto", "bank statement(s)"),
    "what.papers": ("papier(s)", "Papier(e)", "documento/i", "paper(s)"),
}

MONTH_NAMES: dict[str, tuple[str, ...]] = {
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember"),
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
           "settembre", "ottobre", "novembre", "dicembre"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"),
}


def norm_lang(lang: str | None) -> str:
    """Ramène n'importe quelle valeur à une langue connue (fr par défaut)."""
    if not lang:
        return DEFAULT_LANG
    lang = lang.lower()[:2]
    return lang if lang in LANGS else DEFAULT_LANG


def t(key: str, lang: str | None = None, **kw) -> str:
    """Traduit une clé ; les {variables} sont remplies avec kw ; clé inconnue → la clé."""
    row = _S.get(key)
    if row is None:
        return key
    text = row[LANGS.index(norm_lang(lang))]
    if kw:
        try:
            text = text.format(**kw)
        except (KeyError, IndexError):
            pass
    return text


def month_label(month: str, lang: str | None = None) -> str:
    """'2026-09' → 'septembre 2026' dans la langue voulue."""
    try:
        y, m = month.split("-")[:2]
        return f"{MONTH_NAMES[norm_lang(lang)][int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return month


def all_strings(lang: str | None = None) -> dict[str, str]:
    """Toutes les chaînes d'une langue, pour la page (une seule requête JSON)."""
    i = LANGS.index(norm_lang(lang))
    return {k: v[i] for k, v in _S.items()}
