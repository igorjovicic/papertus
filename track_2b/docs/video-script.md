# Paperasse — demo video script (≤ 2 minutes, captions, no voice needed)

Screen: the Paperasse page at 1280 px, dark captions bar at the bottom. Cuts, no transitions.

| t | Screen | Caption (EN, one line; FR version below) |
|---|---|---|
| 0:00 | A Swiss QR-bill PDF on screen, then a plain chat window with Apertus answering "The amount is CHF 734.50" (wrong: it subtracted the deposit) | **Swiss papers carry their own truth. Models guess anyway.** |
| 0:08 | Paperasse page, empty, a folder of 100 PDFs dragged onto the drop zone | **Paperasse: drop a folder of Swiss papers.** |
| 0:16 | The list fills; a QR-bill is selected: amount ✓ "qr", IBAN ✓ "iban", reference ✓ "qrr", due date ✓ "text" | **Code decodes the QR and checks every figure. Apertus only reads the words.** |
| 0:30 | A bank statement: movements ✓, closing balance ✓ "arithmetic: opening + movements = closing" | **A statement must add up. If it does, the balance is proven.** |
| 0:40 | The unbalanced statement: closing balance blank, grey reason "arithmetic: … statement says 1'988.80" | **If it doesn't, the field stays blank, with the reason. Never a guess.** |
| 0:52 | Rapport tab, September 2026, French; switch to Deutsch then Italiano | **One click: the month's report, in your language. Tables by code, one sentence by Apertus, every number checked.** |
| 1:08 | Ask box: "Combien dois-je encore à Schreinerei Huber AG ?" → answer with [p017] link; click opens the PDF | **Ask. The answer cites the paper. Click it.** |
| 1:20 | Ask: "Quel est le solde de l'Alpenbank au 30 septembre ?" → "Je ne trouve pas ce chiffre dans vos papiers" + reason | **When the paper can't prove it, Paperasse says so.** |
| 1:30 | docs/results.md table on screen, zoom on the "invented" row: Baseline 4.7 % → Ours 0.0 %; questions 25 % → 96 % | **100 papers, 50 questions. Apertus alone invents 4.7 % of the figures. Paperasse: 0.0 %.** |
| 1:42 | Terminal: `make run`, container starts, `.env` with `LLM_BASE_URL=http://host.docker.internal:8080/v1`, Wi-Fi icon off | **One container. Apertus on a Swiss cloud, or offline on your own machine. Nothing leaves.** |
| 1:52 | Title card: Paperasse · Apache-2.0 · github.com/igorjovicic/paperasse | **Swiss people can talk to their papers with Swiss technology. And the answer is never a guess.** |

French captions (for a second cut if wanted): « Les papiers suisses portent leur propre vérité. Les modèles devinent quand même. » · « Déposez un dossier de papiers suisses. » · « Le code décode le QR et contrôle chaque chiffre. Apertus ne lit que les mots. » · « Un relevé doit tomber juste. S'il tombe juste, le solde est prouvé. » · « Sinon, le champ reste vide, avec la raison. Jamais une devinette. » · « Un clic : le rapport du mois, dans votre langue. » · « Demandez. La réponse cite le papier. Cliquez. » · « Quand le papier ne peut pas le prouver, Paperasse le dit. » · « 100 papiers, 50 questions. Apertus seul invente 4,7 % des chiffres. Paperasse : 0,0 %. » · « Un conteneur. Apertus sur un cloud suisse, ou hors ligne chez vous. Rien ne sort. » · « Parler à ses papiers avec une technologie suisse. Et la réponse n'est jamais une devinette. »

Production: screen recording with the browser pane at 1280×800 (QuickTime or `ffmpeg -f avfoundation`), captions burned with ffmpeg `drawtext` (Helvetica, white on #111 bar, 36 px), 30 fps, H.264, ≤ 60 MB. Numbers in captions must be copied from the final docs/results.md.
