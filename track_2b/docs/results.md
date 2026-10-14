# Résultats : Apertus seul contre Apertus + code

Modèle : `swiss-ai/Apertus-v1.5-70B` · date : 2026-10-14 · papiers : Baseline 100, Ours 100.

Champs comptés : montants, dates, IBAN, références, numéros (les clés de `schema.scored_keys`). « Inventé » = une valeur non vide qui diffère de la vérité (ou une valeur là où la vérité exige un blanc). Questions : la réponse contient le chiffre, la date ou la liste exacte attendue.

| Setup | Metric | Result |
|-------|--------|--------|
| Baseline | fields correct | 88.7 % |
| Baseline | fields invented (headline, lower is better) | 4.7 % |
| Baseline | fields blank | 6.5 % |
| Baseline | kind correct | 91.0 % |
| Baseline | questions exact match (20 q) | 25.0 % |
| Baseline | tokens per paper | 1454 |
| Baseline | seconds per paper | 6.1 |
| Ours | fields correct | 99.6 % |
| Ours | fields invented (headline, lower is better) | 0.0 % |
| Ours | fields blank | 0.4 % |
| Ours | kind correct | 100.0 % |
| Ours | questions exact match (50 q) | 96.0 % |
| Ours | questions exact match on the Baseline's 20 q | 95.0 % |
| Ours | tokens per paper | 1110 |
| Ours | seconds per paper | 6.5 |

## Par genre de papier

| Kind | Setup | Papers | Correct | Invented | Blank |
|------|-------|--------|---------|----------|-------|
| bank_statement | Baseline | 30 | 95.3 % | 4.7 % | 0.0 % |
| bank_statement | Ours | 30 | 98.7 % | 0.0 % | 1.3 % |
| official_letter | Baseline | 30 | 70.0 % | 0.0 % | 30.0 % |
| official_letter | Ours | 30 | 100.0 % | 0.0 % | 0.0 % |
| qr_bill | Baseline | 40 | 93.2 % | 6.8 % | 0.0 % |
| qr_bill | Ours | 40 | 100.0 % | 0.0 % | 0.0 % |

## Par champ (inventé / total)

| Field | Baseline | Ours |
|-------|-------|-------|
| bank_statement.account_iban | 1 / 30 | 0 / 30 |
| bank_statement.closing_balance | 6 / 30 | 0 / 30 |
| bank_statement.movements | 28 / 30 | 0 / 30 |
| bank_statement.opening_balance | 0 / 30 | 0 / 30 |
| bank_statement.period_end | 0 / 30 | 0 / 30 |
| bank_statement.period_start | 0 / 30 | 0 / 30 |
| official_letter.amount_chf | 0 / 30 | 0 / 30 |
| official_letter.deadline | 0 / 30 | 0 / 30 |
| official_letter.letter_date | 0 / 30 | 0 / 30 |
| official_letter.reference_number | 0 / 30 | 0 / 30 |
| qr_bill.amount_chf | 0 / 40 | 0 / 40 |
| qr_bill.due_date | 0 / 40 | 0 / 40 |
| qr_bill.iban | 0 / 40 | 0 / 40 |
| qr_bill.invoice_date | 0 / 40 | 0 / 40 |
| qr_bill.invoice_number | 0 / 40 | 0 / 40 |
| qr_bill.reference | 10 / 40 | 0 / 40 |
| qr_bill.reference_type | 9 / 40 | 0 / 40 |
