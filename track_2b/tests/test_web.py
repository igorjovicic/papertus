"""L'application : la page, la liste, la santé, le rapport et la question sans modèle."""

import pytest
from fastapi.testclient import TestClient

from paperasse import web
from paperasse.schema import Facts, Field, Paper


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
    assert r.status_code == 200 and "Paperasse" in r.text and "<script" in r.text
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
    assert rep["report"]["totals"]["bills_total"] == 1234.5 and "Bills received" in rep["html"]
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
        assert client.get(f"/api/strings?lang={lang}").json()["tab.report"] == word
    assert "ß" not in "".join(client.get("/api/strings?lang=de").json().values())
