# Paperasse

**Swiss papers in, verified facts out, a report, then a conversation.** Built with Apertus 1.5 for [Hack Apertus 2026](https://hackapertus.ch/), Track 2B (own project).

Drop a folder of Swiss papers (QR-bills, bank statements, official letters, in French, German or Italian). Apertus reads the words; plain code decodes the QR, checks every checksum, redoes every sum and compares every date. Every figure on screen points back to the paper it came from, and a field the code cannot confirm stays blank with its reason, never a guess. Then: the month's report in your language, and answers to your questions with the paper cited.

On 100 synthetic Swiss papers, Apertus 1.5 70B alone invents 4.5 % of the figures. Paperasse: 0.0 %.

The project lives in [`track_2b/`](track_2b/) (the hackathon's template layout): [README](track_2b/README.md) for the quick start (`make run`), [technical report](track_2b/technical_report.md) for the architecture and the numbers, [results](track_2b/docs/results.md) for the full tables.

Licences: code Apache-2.0, documentation CC-BY-4.0, data CDLA-Permissive-2.0.
