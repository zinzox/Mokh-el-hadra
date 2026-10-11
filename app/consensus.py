"""Server-side academic search using the official Consensus REST contract.

https://github.com/Consensus-NLP/consensus-api
Only the submitted research question is sent; no triage/watch data is included.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
SEARCH_URL = "https://api.consensus.app/v1/search"
TIMEOUT_SECONDS = 25


class ConsensusError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def api_key() -> str:
    configured = os.environ.get("CONSENSUS_API_KEY")
    if configured is not None:
        return configured.strip()
    try:
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.strip().partition("=")
            if separator and name.strip() == "CONSENSUS_API_KEY":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value.strip()
    except OSError:
        pass
    return ""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authentication header to another host.
        return None


def search_papers(query: str, key: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"query": query, "page_size": 5})
    request = urllib.request.Request(
        f"{SEARCH_URL}?{params}",
        headers={"x-api-key": key, "Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        messages = {
            401: (502, "Consensus a refusé la clé API. Vérifiez la configuration du serveur."),
            403: (502, "L’accès à Consensus est refusé pour ce compte."),
            402: (503, "L’accès à Consensus est suspendu. Vérifiez la facturation du compte."),
            429: (429, "Limite Consensus atteinte. Réessayez plus tard ou vérifiez le quota de votre compte."),
            422: (400, "Consensus n’a pas pu traiter cette question. Essayez de la reformuler."),
        }
        status, message = messages.get(exc.code, (502, "Consensus est temporairement indisponible. Réessayez plus tard."))
        raise ConsensusError(status, message) from None
    except (TimeoutError, socket.timeout):
        raise ConsensusError(504, "Consensus met trop de temps à répondre. Réessayez dans quelques instants.") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ConsensusError(504, "Consensus met trop de temps à répondre. Réessayez dans quelques instants.") from None
        raise ConsensusError(502, "Connexion à Consensus impossible. Vérifiez la connexion Internet du serveur.") from None
    except OSError:
        raise ConsensusError(502, "Connexion à Consensus impossible. Vérifiez la connexion Internet du serveur.") from None
    except (ValueError, UnicodeError):
        raise ConsensusError(502, "Consensus a retourné une réponse illisible. Réessayez plus tard.") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ConsensusError(502, "La réponse de Consensus ne contient pas de publications exploitables.")
    return data


def safe_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return value.strip()


def normalize_papers(data: dict[str, Any]) -> list[dict[str, Any]]:
    papers = []
    for paper in data["results"][:5]:
        if not isinstance(paper, dict) or not isinstance(paper.get("title"), str) or not paper["title"].strip():
            continue
        def text(name: str, limit: int) -> str:
            value = paper.get(name)
            return value.strip()[:limit] if isinstance(value, str) else ""
        doi = text("doi", 300)
        url = safe_link(paper.get("url"))
        if not url and doi.startswith("10.") and "/" in doi:
            url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
        papers.append({
            "reference": len(papers) + 1,
            "title": text("title", 600),
            "journal_name": text("journal_name", 200),
            "publish_year": paper.get("publish_year") if isinstance(paper.get("publish_year"), int) else None,
            "study_type": text("study_type", 100),
            "takeaway": text("takeaway", 1200),
            "abstract": text("abstract", 2500),
            "url": url,
            "doi": doi,
        })
    return papers


class ConsensusClient:
    def __init__(self):
        self.cache: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()
        self.lock = asyncio.Lock()
        self.last_request_at = 0.0

    async def search(self, query: str) -> dict[str, Any]:
        key = api_key()
        if not key:
            raise ConsensusError(503, "Consensus n’est pas configuré. Ajoutez la clé API dans la configuration du serveur.")
        query = " ".join(query.split())
        cache_key = (hashlib.sha256(key.encode()).hexdigest(), query.casefold())
        async with self.lock:
            cached = self.cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 600:
                self.cache.move_to_end(cache_key)
                return copy.deepcopy(cached[1]) | {"query": query, "cached": True}
            # Consensus permits one request per second. Waiting and HTTP work
            # are asynchronous so live measurements continue to update.
            delay = 1.05 - (time.monotonic() - self.last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request_at = time.monotonic()
            papers = normalize_papers(await asyncio.to_thread(search_papers, query, key))
            answer = (
                f"J’ai trouvé {len(papers)} publication{'s' if len(papers) > 1 else ''} pour votre question. "
                "Voici les points clés fournis par Consensus, avec leurs sources."
                if papers else "Aucune publication trouvée pour cette question. Essayez des termes plus précis ou une formulation en anglais."
            )
            result = {"query": query, "answer": answer, "papers": papers, "provider": "consensus", "cached": False}
            self.cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
            self.cache.move_to_end(cache_key)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result
