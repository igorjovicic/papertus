"""Client minimal pour Apertus 1.5 via un point d'accès compatible OpenAI.

Trois variables d'environnement suffisent : LLM_NAME, LLM_BASE_URL, LLM_API_KEY.
Aucune adresse ni clé n'est écrite dans le code. Le même client fonctionne contre
le cloud souverain (Infomaniak) ou contre un serveur local compatible OpenAI
(vLLM ou équivalent) servant Apertus 8B, hors ligne.

Ce module compte aussi les jetons consommés (compteur au niveau du module) pour
que l'évaluation puisse rapporter le coût par papier.
"""

from __future__ import annotations

import json as _json
import os
import random
import threading
import time
from typing import Any

import httpx

DEFAULT_MODEL = "swiss-ai/Apertus-v1.5-70B"
TIMEOUT_S = 120.0
RETRIES = 4

# Compteur de jetons, partagé par tout le processus (protégé par un verrou :
# l'évaluation appelle le modèle depuis plusieurs fils).
_lock = threading.Lock()
_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

# Compteur par fil d'exécution : l'évaluation attribue les jetons à chaque papier.
_local = threading.local()

# Mémorise si le serveur refuse response_format, pour ne pas réessayer à chaque appel.
_json_mode_supported: bool | None = None


class ModelError(RuntimeError):
    """Le point d'accès n'a pas répondu correctement après les tentatives."""


class ModelAnswerError(ValueError):
    """Le modèle a répondu, mais pas en JSON exploitable."""


def _settings() -> tuple[str, str, str]:
    name = os.getenv("LLM_NAME") or DEFAULT_MODEL
    base = (os.getenv("LLM_BASE_URL") or "").rstrip("/")
    key = os.getenv("LLM_API_KEY") or ""
    if not base:
        raise ModelError("LLM_BASE_URL is not set")
    return name, base, key


def usage_total() -> dict[str, int]:
    """Copie du compteur de jetons cumulé depuis le démarrage du processus."""
    with _lock:
        return dict(_usage)


def usage_reset() -> None:
    with _lock:
        for k in _usage:
            _usage[k] = 0


def _add_usage(u: dict[str, Any] | None) -> dict[str, int]:
    got = {
        "prompt_tokens": int((u or {}).get("prompt_tokens") or 0),
        "completion_tokens": int((u or {}).get("completion_tokens") or 0),
    }
    got["total_tokens"] = int((u or {}).get("total_tokens") or (got["prompt_tokens"] + got["completion_tokens"]))
    with _lock:
        for k, v in got.items():
            _usage[k] += v
        _usage["calls"] += 1
    for scope in getattr(_local, "scopes", []):
        for k, v in got.items():
            scope[k] += v
        scope["calls"] += 1
    return got


class usage_scope:
    """`with llm.usage_scope() as u:` → u compte les jetons des appels faits dans ce fil."""

    def __enter__(self) -> dict[str, int]:
        self.counter = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        if not hasattr(_local, "scopes"):
            _local.scopes = []
        _local.scopes.append(self.counter)
        return self.counter

    def __exit__(self, *exc: Any) -> None:
        _local.scopes.remove(self.counter)


def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    """Un POST avec trois reprises (attente croissante) sur 429, 5xx et délais dépassés."""
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                r = client.post(url, headers=headers, json=body)
            if r.status_code == 429 or r.status_code >= 500:
                last = ModelError(f"HTTP {r.status_code}: {r.text[:200]}")
            else:
                return r
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
        if attempt < RETRIES:
            time.sleep((2 ** attempt) + random.random())
    raise ModelError(f"endpoint failed after {RETRIES + 1} attempts: {last}")


def chat(
    messages: list[dict[str, str]],
    *,
    json: bool = True,
    max_tokens: int = 1024,
    temperature: float = 0,
) -> tuple[str, dict[str, int]]:
    """Un tour de conversation. Retourne (texte, usage) ; usage = jetons de cet appel.

    Avec json=True on demande le mode JSON du serveur (response_format) ; s'il est
    refusé (HTTP 400), on recommence sans et on s'en souvient.
    """
    global _json_mode_supported
    name, base, key = _settings()
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body: dict[str, Any] = {
        "model": name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    want_json_mode = json and _json_mode_supported is not False
    if want_json_mode:
        body["response_format"] = {"type": "json_object"}

    r = _post(url, headers, body)
    if r.status_code == 400 and want_json_mode:
        # Le serveur ne connaît pas response_format : on retire l'option, une fois pour toutes.
        _json_mode_supported = False
        body.pop("response_format", None)
        r = _post(url, headers, body)
    elif want_json_mode and r.status_code == 200:
        _json_mode_supported = True
    if r.status_code != 200:
        raise ModelError(f"HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise ModelError(f"unexpected answer shape: {str(data)[:200]}") from e
    usage = _add_usage(data.get("usage"))
    return text, usage


# ---------------------------------------------------------------------------
# JSON : nettoyage et réparation d'une réponse tronquée
# ---------------------------------------------------------------------------

def strip_fences(text: str) -> str:
    """Retire les clôtures ```json ... ``` et tout ce qui précède la première accolade."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start = t.find("{")
    if start > 0:
        t = t[start:]
    return t.strip()


def repair_json(text: str) -> str:
    """Ferme les chaînes, crochets et accolades laissés ouverts par une réponse coupée.

    On parcourt le texte en suivant l'état (dans une chaîne ? échappement ?) et la
    pile des ouvertures ; à la fin on retire une virgule ou un deux-points pendant,
    puis on referme dans l'ordre inverse.
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    out = text
    if in_str:
        out += '"'
    # Une clé sans valeur ou une virgule finale rendent le JSON invalide : on nettoie.
    stripped = out.rstrip()
    while stripped and stripped[-1] in ",:":
        if stripped[-1] == ":":
            # on retire aussi la clé orpheline : …,"key": → …
            q = stripped.rfind('"', 0, len(stripped) - 1)
            q2 = stripped.rfind('"', 0, q) if q > 0 else -1
            stripped = stripped[:q2].rstrip() if q2 >= 0 else stripped[:-1].rstrip()
            while stripped and stripped[-1] == ",":
                stripped = stripped[:-1].rstrip()
        else:
            stripped = stripped[:-1].rstrip()
    out = stripped
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def parse_json_object(text: str) -> dict[str, Any]:
    """Texte du modèle → dict. Essaie tel quel, puis après nettoyage, puis réparé."""
    candidates = [text, strip_fences(text)]
    candidates.append(repair_json(candidates[-1]))
    last_err: Exception | None = None
    for c in candidates:
        if not c:
            continue
        try:
            obj = _json.loads(c)
        except ValueError as e:
            last_err = e
            continue
        if isinstance(obj, dict):
            return obj
        last_err = ModelAnswerError(f"JSON is not an object: {type(obj).__name__}")
    raise ModelAnswerError(f"unparsable model answer: {last_err}; text={text[:200]!r}")


def json_answer(
    messages: list[dict[str, str]],
    schema_hint: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0,
) -> dict[str, Any]:
    """Demande un objet JSON (et rien d'autre) décrit par schema_hint, le parse, le répare.

    Lève ModelAnswerError si la réponse n'est pas un objet JSON exploitable.
    """
    sys_line = (
        "You answer with one JSON object only, no prose, no code fences. "
        "Keys and shape: " + schema_hint
    )
    msgs = [{"role": "system", "content": sys_line}] + list(messages)
    text, _ = chat(msgs, json=True, max_tokens=max_tokens, temperature=temperature)
    return parse_json_object(text)
