"""Offline replay harness for the HTTP-facing half of the collectors.

WHY THIS MODULE EXISTS
-----------------------
The scraping layer talks to live services: ``providers_openalex`` and
``provider_arxiv`` call JSON APIs, ``journal_abstracts`` calls Europe PMC and
publisher landing pages, ``jmlr`` reads jmlr.org's own HTML index.  When one of
those services changes shape -- a renamed JSON field, a publisher that moves
``<p class="abstract">`` -- the harvest does **not** fail.  It returns less,
and a quietly empty harvest is indistinguishable from a quiet year.  Today
nothing in the suite notices, because every parser is either untested or
tested against a hand-written string that nobody compares with the live
response again.

This module makes those responses replayable **offline**:

* :func:`record` / :func:`write_fixture` capture a response as a small JSON
  fixture on disk;
* :func:`load_fixture_dir` reads a directory of fixtures back;
* :class:`ReplayTransport` is an ``httpx`` transport that answers requests
  from the recorded table and raises ``httpx.ConnectError`` when nothing
  matches -- a missing fixture is a loud failure, never a silent empty page;
* :class:`ReplayClient` wires that transport into a real ``httpx.Client`` so
  the code under test is the production code, byte for byte; only the origin
  of the bytes changes.

The fixtures are deliberately small but **structurally faithful**: they carry
the awkward parts of the real payloads (OpenAlex's inverted abstract index,
Europe PMC's ``resultList.result`` envelope, jmlr.org's ``<dl>`` entries).
A tidy synthetic payload would not notice a shape change; these do.

Typical use in a test::

    from paper_ingest.replay import ReplayClient, load_fixture_dir

    client = ReplayClient(directory=Path("tests/fixtures"))
    provider = OpenAlexProvider(mailto="test@example.org", client=client)
    papers = provider.search_source("neurips", 2023)

and for a module that builds its own client (jmlr does), the seam is the
client factory -- see ``tests/test_replay.py`` for the working example.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

__all__ = [
    "RecordedResponse",
    "ReplayTransport",
    "ReplayClient",
    "load_fixture_dir",
    "record",
    "write_fixture",
    "fixture_name",
]

LOGGER = logging.getLogger("paper_ingest.replay")

#: Name of the optional manifest that maps a recorded payload file onto the
#: URL it answers.  See :func:`load_fixture_dir` for the file shapes.
MANIFEST_NAME = "manifest.json"

#: ``GET__api.openalex.org__works.json`` -- the method is the first token.
#: The URL itself lives in the file body, so the name only has to carry the
#: method (and be unique/greppable).
_FIXTURE_NAME_RE = re.compile(r"^(?P<method>[A-Za-z]+)__.*\.json$")


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _url_key(url: str) -> tuple[str, str, str]:
    """Split a URL into ``(scheme://host, path, sorted query)``.

    This is the identity a fixture is matched on.  The query is *sorted* so a
    recording made with ``?b=2&a=1`` still answers a request for
    ``?a=1&b=2``; the path drops a trailing slash so ``/papers/v24/`` and
    ``/papers/v24`` are the same resource.
    """
    parts = urlsplit(str(url))
    host = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return host, path, query


def _normalise_key(key: Any) -> tuple[str, str]:
    """Accept a ``(method, url)`` pair or a bare URL as a fixture-table key."""
    if isinstance(key, tuple) and len(key) == 2:
        method, url = key
        return str(method).upper(), str(url)
    return "GET", str(key)


class _Headers(dict):
    """A case-insensitive ``dict`` of response headers.

    Why not ``httpx.Headers``?  Two reasons.  The production code reads
    headers case-insensitively (``response.headers.get("Retry-After")`` in
    ``providers_openalex``, ``.get("retry-after")`` in ``provider_arxiv``), so
    the mapping must ignore case.  And a harness is meant to be *inspected*
    from a test: ``response.headers == {"content-type": "application/json"}``
    should simply hold, which is not true of a ``MutableMapping``.

    ``multi_items()`` is kept because ``httpx.Cookies.extract_cookies`` calls
    it on every response that passes through an ``httpx.Client``; without it
    the client machinery raises ``AttributeError`` before the parser is ever
    reached.
    """

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        # lower-cased name -> name as it was recorded
        self._original: dict[str, str] = {}
        for key, value in (values or {}).items():
            self[key] = value

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self._original[str(key).lower()] = str(key)

    def __getitem__(self, key: str) -> Any:
        original = self._original.get(str(key).lower())
        if original is not None:
            key = original
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            original = self._original.get(key.lower())
            if original is not None:
                return super().__contains__(original)
        return super().__contains__(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Case-insensitive ``dict.get``."""
        try:
            return self[key]
        except KeyError:
            return default

    def get_list(self, key: str, split_commas: bool = False) -> list[str]:
        """``httpx.Headers.get_list``-alike (used by some cookie paths)."""
        if key not in self:
            return []
        value = self[key]
        if split_commas:
            return [part.strip() for part in str(value).split(",")]
        return [str(value)]

    def multi_items(self) -> list[tuple[str, Any]]:
        """``httpx.Headers.multi_items``-alike."""
        return list(self.items())


# --------------------------------------------------------------------------- #
# The recorded response                                                       #
# --------------------------------------------------------------------------- #
class RecordedResponse(httpx.Response):
    """A recorded HTTP response, usable anywhere an ``httpx.Response`` is.

    It subclasses ``httpx.Response`` on purpose: the transport contract of
    ``httpx`` requires a real response object (the client asserts on
    ``response.stream``, replaces ``response.request``, extracts cookies from
    ``response.headers`` and logs ``response.reason_phrase``).  Handing httpx a
    look-alike duck type would work until the first internal detail drifted,
    so the recorded response *is* an httpx response, carrying recorded bytes.

    Attributes
    ----------
    status_code
        The recorded status code.
    text / content
        The recorded body, as text and as UTF-8 bytes.
    url
        The URL the response belongs to (the request's URL once the response
        travels through an ``httpx.Client``).
    headers
        The recorded headers, as a case-insensitive ``dict``.
    json()
        Parses the body as JSON; raises ``ValueError`` when it is not JSON.
    raise_for_status()
        Raises ``httpx.HTTPStatusError`` for a 4xx/5xx status, exactly like
        the real thing, so production code that checks the status keeps
        working unchanged.
    """

    def __init__(
        self,
        url: str,
        *,
        status_code: int = 200,
        text: str = "",
        headers: Mapping[str, Any] | None = None,
        request: httpx.Request | None = None,
        method: str = "GET",
    ) -> None:
        body = text.encode("utf-8") if isinstance(text, str) else bytes(text or b"")
        # A request is always attached: ``Response.url`` and
        # ``Response.raise_for_status`` need one, and a recorded response that
        # cannot be asked "which URL was this?" is not much of a recording.
        request = request or httpx.Request(method.upper(), str(url))
        super().__init__(status_code=int(status_code), content=body, request=request)
        # The recorded headers replace the ones httpx derived (Content-Length,
        # Content-Type): the recording is the source of truth for the *shape*
        # of the response, and a test that compares ``response.headers`` with
        # the recording must not see anything extra.
        self.headers = _Headers({str(k): str(v) for k, v in (headers or {}).items()})


# --------------------------------------------------------------------------- #
# The transport                                                               #
# --------------------------------------------------------------------------- #
class ReplayTransport(httpx.BaseTransport):
    """An ``httpx`` transport that answers from a table of recorded responses.

    Matching
    --------
    A request is matched on ``(method, scheme://host, path, sorted query)``.

    When no fixture matches the exact query, the transport falls back to the
    **path**: a fixture recorded for ``/works`` answers a request for
    ``/works?filter=...&mailto=...``.  Real clients add parameters that were
    not in the recording (a polite ``mailto``, a pagination cursor, a
    ``select`` projection), and a harness that demanded an exact query match
    would make every recording useless the moment a parameter changed.

    When nothing matches at all the transport raises ``httpx.ConnectError``.
    That is the whole point: a missing fixture must fail loudly.  Returning an
    empty ``200`` body would turn a broken parser into a silently empty
    harvest -- exactly the failure mode this harness exists to catch.
    """

    def __init__(
        self,
        fixtures: Mapping[Any, Mapping[str, Any]] | None = None,
        *,
        directory: Path | str | None = None,
    ) -> None:
        super().__init__()
        #: The fixture table, ``(method, url) -> record``.
        self.fixtures: dict[tuple[str, str], dict[str, Any]] = {}
        if directory is not None:
            self.fixtures.update(load_fixture_dir(Path(directory)))
        for key, value in (fixtures or {}).items():
            method, url = _normalise_key(key)
            entry = dict(value)
            # A record may state its own method; the table key is only the
            # default, so the two can never disagree about the verb.
            method = str(entry.get("method", method)).upper()
            self.fixtures[(method, url)] = entry
        self._exact: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        self._paths: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for (method, url), value in self.fixtures.items():
            host, path, query = _url_key(url)
            self._exact.setdefault((method, host, path, query), value)
            self._paths.setdefault((method, host, path), value)
        #: ``(method, url)`` of every request that was served.
        self.used: list[tuple[str, str]] = []
        #: ``(method, url)`` of the *fixture* that answered each request.
        self.matched: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Answer ``request`` from the recorded table.

        Raises ``httpx.ConnectError`` when no fixture matches -- never a
        silent empty body.
        """
        method = request.method.upper()
        url = str(request.url)
        host, path, query = _url_key(url)
        entry = self._exact.get((method, host, path, query))
        if entry is None:
            entry = self._paths.get((method, host, path))
        if entry is None:
            raise httpx.ConnectError(
                f"no recorded fixture for {method} {url}", request=request
            )
        self.used.append((method, url))
        self.matched.append((method, str(entry.get("url", url))))
        body = entry.get("body", "")
        return RecordedResponse(
            url,
            status_code=int(entry.get("status", 200) or 200),
            text=body if isinstance(body, str) else str(body),
            headers=entry.get("headers") or {},
            request=request,
        )


# --------------------------------------------------------------------------- #
# The client                                                                  #
# --------------------------------------------------------------------------- #
class ReplayClient:
    """An ``httpx.Client`` that talks to a :class:`ReplayTransport`.

    It is a drop-in for ``httpx.Client`` for everything the collectors use:
    ``get``/``request`` return real response objects, ``close`` works, and it
    can be used as a context manager.  Production code that builds its own
    client (jmlr's ``_new_client()``) can return one of these, and the whole
    module then runs offline against the recordings.

    ``transport`` is exposed so a test can assert *which* fixtures were used
    (and, more importantly, that no request escaped to the network).
    """

    def __init__(
        self,
        fixtures: Mapping[Any, Mapping[str, Any]] | None = None,
        *,
        directory: Path | str | None = None,
        transport: ReplayTransport | None = None,
        headers: Mapping[str, str] | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.transport = (
            transport
            if transport is not None
            else ReplayTransport(fixtures, directory=directory)
        )
        # The collectors follow redirects and are generous with timeouts;
        # mirror that so the replay behaves like the real client.
        client_kwargs.setdefault("follow_redirects", True)
        client_kwargs.setdefault("timeout", 60.0)
        self._client = httpx.Client(
            transport=self.transport, headers=headers, **client_kwargs
        )

    # -- the httpx.Client surface the collectors use ---------------------- #
    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """``httpx.Client.get`` -- returns the recorded response."""
        return self._client.get(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """``httpx.Client.request`` -- returns the recorded response."""
        return self._client.request(method, url, **kwargs)

    def close(self) -> None:
        """Close the pooled client.  The transport (and its fixtures) survive."""
        self._client.close()

    def __enter__(self) -> "ReplayClient":
        self._client.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.__exit__(*exc_info)

    # -- introspection ----------------------------------------------------- #
    @property
    def used(self) -> list[tuple[str, str]]:
        """``(method, url)`` of every request served by the transport."""
        return list(self.transport.used)

    @property
    def matched(self) -> list[tuple[str, str]]:
        """``(method, fixture url)`` of every request served by the transport."""
        return list(self.transport.matched)

    @property
    def client(self) -> httpx.Client:
        """The underlying ``httpx.Client`` (for code that wants the real one)."""
        return self._client


# --------------------------------------------------------------------------- #
# Fixture files                                                               #
# --------------------------------------------------------------------------- #
def fixture_name(url: str, method: str = "GET") -> str:
    """The file name a fixture for ``url`` is stored under.

    The rule is ``<METHOD>__<host>__<path with '/' -> '_'>.json``, e.g.
    ``GET__api.openalex.org__works.json``.  The method is part of the name
    because the same URL can be answered differently per method; the query
    string is *not* part of it, because the exact URL (query included) is
    stored in the file body.
    """
    parts = urlsplit(str(url))
    host = parts.netloc or "unknown-host"
    path = parts.path.strip("/") or "root"
    return f"{method.upper()}__{host}__{path.replace('/', '_')}.json"


def record(
    url: str,
    method: str = "GET",
    *,
    status: int = 200,
    headers: Mapping[str, Any] | None = None,
    body: str = "",
) -> dict[str, Any]:
    """Build a fixture record for ``url``.

    A record is the four documented fields: ``url``, ``status``, ``headers``
    and ``body``.  The *method* is deliberately not one of them: the fixture
    table is keyed by ``(method, url)`` and the file name carries the method
    too, so keeping the body to four fields means a recorded file is a
    verbatim copy of the response with no second source of truth to keep in
    sync.  ``method`` is accepted so that a record can be built with the same
    call shape as :func:`write_fixture` and :func:`fixture_name`.
    """
    return {
        "url": str(url),
        "status": int(status),
        "headers": {str(k): str(v) for k, v in (headers or {}).items()},
        "body": body if isinstance(body, str) else str(body),
    }


def write_fixture(
    directory: Path | str,
    url: str,
    *,
    method: str = "GET",
    status: int = 200,
    headers: Mapping[str, Any] | None = None,
    body: str = "",
) -> Path:
    """Write the fixture for ``url`` into ``directory`` and return its path.

    The directory is created when missing, and the file follows the naming
    convention of :func:`fixture_name`, so a directory written by this
    function can be read back by :func:`load_fixture_dir`.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / fixture_name(url, method)
    payload = record(url, method, status=status, headers=headers, body=body)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_fixture_dir(path: Path | str) -> dict[tuple[str, str], dict[str, Any]]:
    """Load every recorded fixture in ``path`` into a ``(method, url)`` table.

    Two file shapes are understood.

    1. **Record files** -- named ``GET__api.openalex.org__works.json`` and
       holding ``{"url", "status", "headers", "body"}``.  The method comes
       from the file name, the exact URL from the body.
    2. **Payload files listed in ``manifest.json``** -- the response exactly
       as it was captured (an OpenAlex JSON document, a jmlr.org HTML page).
       The manifest says which URL such a payload answers::

           {"jmlr/volume24.html": {"url": "https://jmlr.org/papers/v24/",
                                   "status": 200,
                                   "headers": {"content-type": "text/html"}}}

       This exists because the natural shape of a recording *is* the payload:
       an HTML page stays an HTML page, so a diff of the recording shows
       exactly what the publisher changed.

    A file that cannot be parsed is skipped with a warning: one broken
    recording must not take the whole suite down.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"fixture directory not found: {directory}")

    table: dict[tuple[str, str], dict[str, Any]] = {}
    manifest_paths: set[Path] = set()

    manifest_path = directory / MANIFEST_NAME
    if manifest_path.is_file():
        manifest = _read_manifest(manifest_path)
        for relative, spec in manifest.items():
            target = directory / str(relative)
            try:
                manifest_paths.add(target.resolve())
            except OSError:  # pragma: no cover - resolve() is lenient on 3.11
                manifest_paths.add(target)
            try:
                body = target.read_text(encoding="utf-8")
            except OSError as exc:
                LOGGER.warning("skipping fixture %s: %s", target, exc)
                continue
            if not isinstance(spec, Mapping) or not isinstance(spec.get("url"), str):
                LOGGER.warning("skipping manifest entry %s: no 'url' field", relative)
                continue
            method = str(spec.get("method", "GET")).upper()
            table[(method, spec["url"])] = {
                "url": spec["url"],
                "status": int(spec.get("status", 200) or 200),
                "headers": {
                    str(k): str(v) for k, v in (spec.get("headers") or {}).items()
                },
                "body": body,
            }

    for file in sorted(directory.rglob("*.json")):
        if file.name == MANIFEST_NAME:
            continue
        if file.resolve() in manifest_paths:
            continue
        match = _FIXTURE_NAME_RE.match(file.name)
        if not match:
            # Not a fixture file (a raw payload, say) -- the manifest may
            # still list it, otherwise it is simply not a fixture.
            continue
        method = match.group("method").upper()
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("skipping malformed fixture %s: %s", file, exc)
            continue
        if not isinstance(data, Mapping) or not isinstance(data.get("url"), str):
            LOGGER.warning("skipping fixture %s: no 'url' field", file)
            continue
        # A record may state its own method; the file name is only the
        # default, so a hand-written fixture is not silently answered for the
        # wrong verb.
        method = str(data.get("method", method)).upper()
        table[(method, data["url"])] = {
            "url": data["url"],
            "status": int(data.get("status", 200) or 200),
            "headers": {
                str(k): str(v) for k, v in (data.get("headers") or {}).items()
            },
            "body": data.get("body", "") or "",
        }

    return table


def _read_manifest(path: Path) -> Mapping[str, Any]:
    """Read ``manifest.json``; an unreadable manifest is a warning, not a crash."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("ignoring unreadable fixture manifest %s: %s", path, exc)
        return {}
    if not isinstance(data, Mapping):
        LOGGER.warning("fixture manifest %s is not an object; ignoring it", path)
        return {}
    return data
