# Résultats : Apertus seul contre Apertus + code

Modèle (LLM_NAME) : `swiss-ai/Apertus-v1.5-70B` · date : 2026-10-15 · papiers : Baseline 100, Ours 100.

Champs comptés : montants, dates, IBAN, références, numéros (les clés de `schema.scored_keys`). « Inventé » = une valeur non vide qui diffère de la vérité (ou une valeur là où la vérité exige un blanc). Questions : la réponse contient le chiffre, la date ou la liste exacte attendue ; une réponse blanche (« je ne trouve pas ») ne compte juste que si la vérité attend un blanc ; seules les valeurs sont lues, jamais les raisons.

| Setup | Metric | Result |
|-------|--------|--------|
| Baseline | fields correct | 88.9 % |
| Baseline | fields invented (headline, lower is better) | 4.5 % |
| Baseline | fields blank | 6.5 % |
| Baseline | kind correct | 91.0 % |
| Baseline (all texts in one prompt, context capped at 28000 chars) | questions exact match (20 q) | 30.0 % |
| Baseline + retrieval (same papers as Ours, raw text, free-form, no checks) | questions exact match (20 q) | 95.0 % |
| Baseline | tokens per paper | 1456 |
| Baseline | seconds per paper | 6.1 |
| Ours | fields correct | 100.0 % |
| Ours | fields invented (headline, lower is better) | 0.0 % |
| Ours | fields blank | 0.0 % |
| Ours | kind correct | 100.0 % |
| Ours | questions exact match (50 q, product path: ask.py rules first, model for the rest) | 100.0 % |
| Ours | questions answered by rules / by model / none | 50 / 0 / 0 |
| Ours | questions exact match on the Baseline's 20 q | 100.0 % |
| Ours | tokens per paper | 1111 |
| Ours | seconds per paper | 6.4 |

## Par genre de papier

| Kind | Setup | Papers | Correct | Invented | Blank |
|------|-------|--------|---------|----------|-------|
| bank_statement | Baseline | 30 | 96.0 % | 4.0 % | 0.0 % |
| bank_statement | Ours | 30 | 100.0 % | 0.0 % | 0.0 % |
| official_letter | Baseline | 30 | 70.0 % | 0.0 % | 30.0 % |
| official_letter | Ours | 30 | 100.0 % | 0.0 % | 0.0 % |
| qr_bill | Baseline | 40 | 93.2 % | 6.8 % | 0.0 % |
| qr_bill | Ours | 40 | 100.0 % | 0.0 % | 0.0 % |

## Par champ (inventé / total)

| Field | Baseline | Ours |
|-------|-------|-------|
| bank_statement.account_iban | 1 / 30 | 0 / 30 |
| bank_statement.closing_balance | 5 / 30 | 0 / 30 |
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
