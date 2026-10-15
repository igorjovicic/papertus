"""L'application : la page, la liste, la santé, le rapport et la question sans modèle."""

import pytest
from fastapi.testclient import TestClient

from papertus import i18n, web
from papertus.schema import Facts, Field, Paper


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(web, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(web, "CACHE_FILE", tmp_path / "runtime" / "cache.json")
    monkeypatch.setattr(web, "INDEX_FILE", tmp_path / "runtime" / "index.db")
    monkeypatch.setattr(web, "STORE", web.Store())
    with TestClient(web.app) as c:
        yield c


def test_page_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and i18n.APP_NAME in r.text and "<script" in r.text and "{{APP}}" not in r.text
    assert "http://" not in r.text.replace("http://localhost", "") and "https://" not in r.text  # aucune ressource externe
    h = client.get("/health").json()
    assert h["ok"] is True and h["papers"] == 0
    assert client.get("/status").json()["running"] is False


def test_papers_list_report_and_ask(client):
    p = Paper(id="p001", file="p001.pdf", kind="qr_bill", language="fr", text="Facture Rochat 1234.50",
              facts=Facts(fields={"issuer": Field("Fiduciaire Rochat SA", "verified", source="qr"),
                                  "amount_chf": Field(1234.5, "verified", source="qr"),
                                  "invoice_date": Field("2026-09-03", "verified", source="text"),
                                  "due_date": Field("2026-10-03", "model")}))
    web.STORE.add_paper(p, web.PAPERS_DIR / "p001.pdf", "deadbeef")
    ps = client.get("/api/papers").json()
    assert len(ps) == 1 and ps[0]["id"] == "p001" and ps[0]["facts"]["amount_chf"]["status"] == "verified"
    assert client.get("/api/months").json() == ["2026-10", "2026-09"]
    rep = client.get("/api/report?month=2026-09&lang=en&prose=0").json()
    assert rep["report"]["totals"]["bills_total"] == 1234.5 and "Bills received" in rep["html"] and "Apertus" in rep["html"]
    assert client.get("/api/report?month=sept").status_code == 400
    a = client.post("/api/ask", json={"question": "Combien dois-je encore à Fiduciaire Rochat SA ?", "lang": "fr"}).json()
    assert a["by"] == "code" and a["value"] == 1234.5 and a["papers"][0]["id"] == "p001"
    assert client.get("/api/paper/p001/file").status_code == 404  # pas de fichier derrière ce papier de test
    assert client.get("/api/paper/zzz").status_code == 404


def test_upload_rejects_non_pdf(client):
    r = client.post("/papers", files=[("files", ("note.txt", b"hello", "text/plain")),
                                      ("files", ("fake.pdf", b"not a pdf", "application/pdf"))])
    assert r.status_code == 200 and r.json() == {"received": 0, "skipped": 2, "status": r.json()["status"]}


def test_strings_in_four_languages(client):
    for lang, word in (("fr", "Rapport"), ("de", "Bericht"), ("it", "Rapporto"), ("en", "Report")):
        got = client.get(f"/api/strings?lang={lang}").json()
        assert got["tab.report"] == word and got["_app"] == i18n.APP_NAME and "Apertus" in got["_badge"]
    assert "ß" not in "".join(client.get("/api/strings?lang=de").json().values())


def test_seed_is_model_independent_but_runtime_cache_is_not(client, monkeypatch, tmp_path):
    """La graine livrée sert quel que soit LLM_NAME ; le cache d'exécution est propre au modèle."""
    pdf = web.PAPERS_DIR / "p777.pdf"
    web.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 seed test")
    digest = web.file_hash(pdf)
    entry = Paper(id="p777", file="p777.pdf", kind="qr_bill", language="fr", text="x",
                  facts=Facts(fields={"amount_chf": Field(12.5, "verified", source="qr")})).to_dict()
    entry["read_with"] = {"model": "swiss-ai/Apertus-v1.5-70B", "size": "70B", "local": False}
    web.STORE.seed = {web.seed_key(digest): entry}
    monkeypatch.setenv("LLM_NAME", "someone/other-model-8B")
    assert web.seed_key(digest).count(":") == 1 and "other" not in web.seed_key(digest)
    web.STORE.read_one(pdf)                      # trouvé dans la graine : aucune lecture, aucun modèle
    p = client.get("/api/paper/p777").json()
    assert p["facts"]["amount_chf"]["value"] == 12.5 and p["read_with"]["size"] == "70B"
    assert web.cache_key(digest) in web.STORE.cache  # recopié dans le cache d'exécution, clé avec le modèle
    assert web.reader_stamp() != web.reader_hash()


def test_agenda_routes(client, tmp_path):
    bill = Paper(id="p001", file="p001.pdf", kind="qr_bill", language="fr",
                 facts=Facts(fields={"issuer": Field("Gärtnerei Wyss", "verified", source="qr"),
                                     "amount_chf": Field(2276.96, "verified", source="qr"),
                                     "due_date": Field("2026-10-03", "verified", source="text")}))
    web.STORE.add_paper(bill, web.PAPERS_DIR / "p001.pdf", "feed")
    a = client.get("/api/agenda?today=2026-10-01&lang=de").json()
    assert a["figures"]["due_7_total"] == 2276.96 and a["groups"]["week"][0]["id"] == "p001:bill"
    assert a["line"].startswith("1 Rechnung diese Woche zu zahlen")
    assert client.get("/api/agenda?today=october").status_code == 400
    r = client.post("/api/agenda/done", json={"event_id": "p001:bill", "done": True}).json()
    assert r == {"event_id": "p001:bill", "done": True}
    assert (web.RUNTIME_DIR / "state.json").exists() and "p001:bill" in (web.RUNTIME_DIR / "state.json").read_text()
    a2 = client.get("/api/agenda?today=2026-10-01").json()
    assert a2["counts"]["week"] == 0 and a2["counts"]["done"] == 1 and a2["figures"]["due_7_total"] == 0
    assert client.post("/api/agenda/done", json={"event_id": "drop table", "done": True}).status_code == 400
    client.post("/api/agenda/done", json={"event_id": "p001:bill", "done": False})
    ics = client.get("/api/agenda.ics?lang=fr&today=2026-10-01")
    assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:Payer Gärtnerei Wyss · CHF 2'276.96" in ics.text and "TRIGGER:-P2D" in ics.text
    page = client.get("/").text
    assert 'id="tab-agenda"' in page and page.index('id="tab-agenda"') < page.index('id="tab-papers"')
