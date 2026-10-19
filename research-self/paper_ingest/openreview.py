from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

BASE = "https://api2.openreview.net"


def value(content: dict[str, Any], *names: str) -> Any:
    for name in names:
        v = content.get(name)
        if isinstance(v, dict) and "value" in v:
            v = v["value"]
        if v not in (None, ""):
            return v
    return None


class OpenReview:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=45, headers={"User-Agent": "PaperGraph local research tool"})
        self.owns_client = client is None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(6):
            try:
                response = await self.client.get(f"{BASE}{path}", params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 5:
                        response.raise_for_status()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 5:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        raise RuntimeError("OpenReview request exhausted retries")

    async def group(self, group_id: str) -> dict[str, Any] | None:
        result = await self.get("/groups", {"id": group_id, "limit": 1})
        groups = result.get("groups", [])
        return groups[0] if groups else None

    async def discover(self, prefix: str) -> list[dict[str, Any]]:
        result = await self.get("/groups", {"prefix": prefix, "limit": 1000})
        return result.get("groups", [])

    async def notes(self, invitation: str, *, content_field: str = "content.venueid"):
        offset = 0
        limit = 1000
        while True:
            field, value = ("content.venueid", invitation) if "/-/" not in invitation else ("invitation", invitation)
            try:
                result = await self.get("/notes", {field: value, "limit": limit, "offset": offset,
                                                     "sort": "cdate:asc"})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403 or "/-/" not in invitation:
                    raise
                public_id = invitation.rsplit("/-/", 1)[0] + "/Submission"
                result = await self.get("/notes", {"content.venueid": public_id, "limit": limit,
                                                     "offset": offset, "sort": "cdate:asc"})
            batch = result.get("notes", [])
            for note in batch:
                yield note
            if len(batch) < limit:
                break
            offset += len(batch)

    async def public_reviews(self, forum: str) -> list[dict[str, Any]]:
        result = await self.get("/notes", {"forum": forum, "limit": 1000, "details": "replies"})
        return [n for n in result.get("notes", []) if "everyone" in n.get("readers", []) or
                any(str(r).endswith("/Authors") for r in n.get("readers", []))]

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()


def normalize(note: dict[str, Any], year: int, source: str = "openreview") -> dict[str, Any]:
    content = note.get("content", {})
    title = str(value(content, "title") or "").strip()
    forum = str(note.get("forum") or note.get("id") or "")
    author_ids = value(content, "authorids", "author_ids") or []
    authors = value(content, "authors") or []
    if not isinstance(authors, list):
        authors = [str(authors)]
    keywords = value(content, "keywords", "keyword") or []
    if not isinstance(keywords, list):
        keywords = [str(keywords)]
    pdf = value(content, "pdf")
    if pdf and str(pdf).startswith("/"):
        pdf = "https://openreview.net" + str(pdf)
    if not pdf and forum:
        pdf = f"https://openreview.net/pdf?id={quote(forum)}"
    decision = value(content, "decision")
    status = "withdrawn" if "withdraw" in str(decision or "").lower() else ("decided" if decision else "submitted")
    return {
        "id": f"openreview:{forum}", "source": source, "source_paper_id": str(note.get("id") or forum),
        "forum_id": forum, "title": title or "Untitled submission", "abstract": value(content, "abstract"),
        "authors": [str(a) for a in authors], "author_ids": author_ids if isinstance(author_ids, list) else [],
        "keywords": [str(k) for k in keywords], "primary_area": value(content, "primary_area", "subject_area"),
        "pdf_url": str(pdf) if pdf else None, "paper_url": f"https://openreview.net/forum?id={quote(forum)}" if forum else None,
        "submission_year": year, "status": status, "decision": str(decision) if decision else None,
        "created_at_source": note.get("cdate"), "updated_at_source": note.get("mdate"), "raw": note,
    }


def raw_path(data_root: Path, key: str, year: int, forum: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", forum)[:160]
    return data_root / "raw" / "openreview" / key / str(year) / f"{safe}.json"


def write_raw(path: Path, note: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(note, ensure_ascii=False, separators=(",", ":"))
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return
    path.write_text(payload, encoding="utf-8")
