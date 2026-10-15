"""Construit la graine livrée (data/cache.json) sans aucun appel au modèle.

L'évaluation garde, pour chaque papier, la réponse brute du modèle (runtime/cache/ours_raw/pNNN.json :
texte, QR, pages, genre, réponse JSON brute, empreinte texte+QR:modèle). On rejoue dessus la
vérification de code ACTUELLE (read.build_paper → verify.verify), on note avec quel modèle la
lecture a été faite (read_with), et on écrit la graine avec la clé « sha256 du PDF : empreinte du
code de lecture » (sans le nom du modèle : la graine sert quel que soit le modèle configuré).

Usage : python scripts/seed_from_eval.py --raw runtime/cache/ours_raw --papers data/papers --out data/cache.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from papertus import read, web  # noqa: E402  (après l'ajout de src/ au chemin)


def model_of(stamp: str) -> str:
    return stamp.split(":", 1)[1] if ":" in stamp else ""


def read_with_of(model: str) -> dict:
    m = re.search(r"(\d+)\s*B\b", model, flags=re.I)
    return {"model": model, "size": f"{m.group(1)}B" if m else "70B", "local": False}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Graine livrée depuis les réponses brutes de l'évaluation.")
    ap.add_argument("--raw", default="runtime/cache/ours_raw")
    ap.add_argument("--papers", default="data/papers")
    ap.add_argument("--out", default="data/cache.json")
    args = ap.parse_args(argv)

    raw_dir, pdf_dir = Path(args.raw), Path(args.papers)
    seed: dict[str, dict] = {}
    skipped: list[str] = []
    counts = {"verified": 0, "model": 0, "blank": 0}
    for raw_file in sorted(raw_dir.glob("p*.json")):
        pid = raw_file.stem
        pdf = pdf_dir / f"{pid}.pdf"
        if not pdf.is_file():
            skipped.append(f"{pid}: no PDF")
            continue
        got_all = json.loads(raw_file.read_text(encoding="utf-8"))
        got = got_all.get("got") or {}
        if not got.get("raw"):
            skipped.append(f"{pid}: no raw answer ({got.get('error')})")
            continue
        # La réponse brute doit correspondre au PDF livré : même texte, même QR.
        text, qr, pages = read.read_pdf(str(pdf))
        expect = hashlib.sha256((text + "\n" + (qr or "")).encode()).hexdigest()[:16]
        stamp = str(got_all.get("stamp", ""))
        if not stamp.startswith(expect):
            skipped.append(f"{pid}: raw answer is for another version of the PDF")
            continue
        paper = read.build_paper(paper_id=pid, file=pdf.name, **got)
        for f in paper.facts.fields.values():
            counts[f.status] = counts.get(f.status, 0) + 1
        entry = paper.to_dict()
        entry["read_with"] = read_with_of(model_of(stamp))
        seed[web.seed_key(web.file_hash(pdf))] = entry

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    print(f"seed: {len(seed)} papers → {out} ({out.stat().st_size // 1024} KB), reader hash {web.reader_hash()}")
    print(f"fields: verified {counts['verified']} · model {counts['model']} · blank {counts['blank']}")
    models = sorted({e['read_with']['model'] for e in seed.values()})
    print(f"read_with: {models}")
    for s in skipped:
        print("skipped", s)
    return 0 if seed and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
