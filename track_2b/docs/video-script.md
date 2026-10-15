# Papertus — demo video (≤ 2 minutes, captions, no voice)

A slideshow of real screens of the running app, each held 6–12 s, with one caption line at the bottom. Swiss style: white, Helvetica, black captions on a thin red rule. Cuts only.

| t | Screen | Caption |
|---|---|---|
| 0:00 | Title card: « Papertus. » with the red dot; under it: Talk to your own papers, on your own laptop, with a small Swiss model. | **Swiss papers carry their own truth. Models guess anyway.** |
| 0:08 | The empty Papertus page, status line « Apertus 8B · sur cette machine · rien ne sort », a folder of PDFs dropped on the zone | **Drop a folder of Swiss papers. Apertus 8B runs on this laptop. Nothing leaves.** |
| 0:18 | A QR-bill selected: amount ✓ qr, IBAN ✓ iban, reference ✓ qrr, due date ✓ text; big tabular amount | **Code decodes the QR and checks every figure. Apertus only reads the words.** |
| 0:30 | A balanced bank statement: movements ✓, closing balance ✓ « arithmetic » | **A statement must add up. If it does, the balance is proven.** |
| 0:40 | The unbalanced statement p050: closing balance « laissé vide · raison : arithmetic: opening 1517.50 + movements = −907.50, statement says −409.65 » | **If it doesn't, the field stays blank, with the reason. Never a guess.** |
| 0:52 | Rapport tab, September 2026, French, then DE, then IT; the Apertus sentence with the red rule and « Apertus 8B · chaque chiffre contrôlé » | **One click: the month's report in your language. Tables by code, one sentence by Apertus, every number checked.** |
| 1:08 | Ask: chip « Combien dois-je encore à la caisse-maladie ? » → answer with [p0xx] citation | **Ask in French, German or Italian. The answer cites the paper.** |
| 1:20 | Ask: « Quel est le solde de l'Alpenbank au 10 octobre ? » → « Je ne trouve pas ce chiffre dans vos papiers » with the reason | **When the paper can't prove it, Papertus says so.** |
| 1:30 | docs/results.md, zoom on the invented row (70B: 4.5 % → 0.0 %) and the 8B row | **100 papers, 50 questions. Apertus alone invents figures. Papertus: none. On a laptop.** |
| 1:42 | Terminal: `make run`, container up; `.env` with `LLM_BASE_URL=http://host.docker.internal:8080/v1`; Wi-Fi off | **One container. A small Swiss model. Your machine.** |
| 1:52 | Title card: Papertus · Apache-2.0 · github.com/igorjovicic/papertus | **Talk to your papers with Swiss technology. The answer is never a guess.** |

French captions (second cut): « Les papiers suisses portent leur propre vérité. Les modèles devinent quand même. » · « Déposez un dossier de papiers suisses. Apertus 8B tourne sur cet ordinateur. Rien ne sort. » · « Le code décode le QR et contrôle chaque chiffre. Apertus ne lit que les mots. » · « Un relevé doit tomber juste. S'il tombe juste, le solde est prouvé. » · « Sinon, le champ reste vide, avec la raison. Jamais une devinette. » · « Un clic : le rapport du mois, dans votre langue. » · « Demandez en français, allemand ou italien. La réponse cite le papier. » · « Quand le papier ne peut pas le prouver, Papertus le dit. » · « 100 papiers, 50 questions. Apertus seul invente des chiffres. Papertus : aucun. Sur un portable. » · « Un conteneur. Un petit modèle suisse. Votre machine. » · « Parler à ses papiers avec une technologie suisse. La réponse n'est jamais une devinette. »

Production: frames captured from the running app at 1280×800 with the local 8B configured (so the status line reads « sur cette machine »), assembled with ffmpeg (each frame 6–12 s, captions drawn with `drawtext`, Helvetica 34 px, 30 fps, H.264, ≤ 60 MB). The numbers in captions are copied from the final `docs/results.md` and `docs/results-8b.md`.
