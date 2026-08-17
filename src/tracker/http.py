"""HTTP layer: polite, retrying, archiving fetcher with tolerant-TLS support.

Every response body is stored in the content-addressed archive and recorded in
`raw_fetches`, so parsers can re-run offline and revisions are detectable.

`fetch()` is the sequential workhorse. `fetch_many()` adds bounded concurrency
for sources whose windows are hundreds of independent large bodies (the Dutch
Tweede Kamer transcripts are ~1.4 MB each): requests overlap, while archive and
DB writes stay on the calling thread with the sqlite3 connection.
"""

from __future__ import annotations

import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx
from charset_normalizer import from_bytes

from . import archive, db

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "ai-safety-policy-tracker/0.1 (research; contact: lennart@finke.dev)"
)

_last_request_at: dict[str, float] = {}
# _throttle is called from fetch_many's worker threads, so the read-modify-write
# of _last_request_at needs a lock or N threads all see the same stale timestamp
# and fire simultaneously, ignoring rate_per_host entirely
_throttle_lock = threading.Lock()


def _tolerant_ssl_context() -> ssl.SSLContext:
    """Permissive TLS for Chinese government hosts with legacy cert chains.

    Used only for allowlisted .gov.cn-style hosts; provenance is unaffected
    because we archive the exact bytes received.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    return ctx


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    text: str
    encoding: str
    content_sha256: str
    raw_fetch_id: int
    from_cache: bool = False


class Fetcher:
    """Archiving fetcher bound to one source name and one DB connection."""

    def __init__(
        self,
        conn,
        source: str,
        *,
        rate_per_host: float = 1.0,
        tolerant_tls: bool = False,
        timeout: float = 30.0,
        extraction_method: str = "direct",
    ):
        self.conn = conn
        self.source = source
        self.rate = rate_per_host
        self.extraction_method = extraction_method
        verify = _tolerant_ssl_context() if tolerant_tls else True
        self.client = httpx.Client(
            headers={"User-Agent": UA},
            timeout=timeout,
            follow_redirects=True,
            verify=verify,
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _throttle(self, host: str):
        """Space request *starts* at rate_per_host, from any number of threads.

        The sleep happens outside the lock: holding it would serialize the whole
        pool onto one in-flight request and undo fetch_many's concurrency. We
        reserve a slot under the lock, then wait for it.
        """
        min_gap = 1.0 / self.rate
        with _throttle_lock:
            slot = max(_last_request_at.get(host, 0.0) + min_gap, time.monotonic())
            _last_request_at[host] = slot
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _decode(self, resp: httpx.Response) -> tuple[str, str]:
        """Decode with header charset first, charset-normalizer fallback (GB2312/GBK archives)."""
        declared = resp.charset_encoding
        if declared:
            try:
                return resp.content.decode(declared), declared
            except (UnicodeDecodeError, LookupError):
                pass
        best = from_bytes(resp.content).best()
        if best is not None:
            return str(best), best.encoding
        return resp.content.decode("utf-8", errors="replace"), "utf-8"

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body=None,
        params=None,
        headers=None,
        retries: int = 3,
        cache: bool = True,
    ) -> FetchResult:
        """Fetch with retry/backoff; archive body; record raw_fetches row.

        cache=True returns the most recent successful archived fetch of the same
        URL+body instead of re-hitting the network (used for offline re-parses
        and idempotent re-runs of fetch windows).
        """
        body_repr = db.j(json_body) if json_body is not None else None
        hit = self._cached(url) if (cache and method == "GET" and params is None) else None
        if hit is not None:
            return hit
        resp, last_exc = self._request(
            url,
            method=method,
            json_body=json_body,
            params=params,
            headers=headers,
            retries=retries,
        )
        if resp is None:
            self._record_failure_soft(url, last_exc, method=method, body_repr=body_repr)
            raise ConnectionError(f"fetch failed after {retries} attempts: {url}: {last_exc}")
        return self._record(resp, method, body_repr)

    # -- fetch internals, split so fetch_many can run _request off-thread ------

    def _cached(self, url: str) -> FetchResult | None:
        row = self.conn.execute(
            "SELECT id, status_code, content_sha256, encoding FROM raw_fetches "
            "WHERE url=? AND source=? AND status_code=200 AND content_sha256 IS NOT NULL "
            "ORDER BY fetched_at DESC LIMIT 1",
            (url, self.source),
        ).fetchone()
        if not row or not archive.exists(self.source, row["content_sha256"]):
            return None
        # commit even on cache hits: parsers interleave CPU-heavy work
        # between fetches, and this is the only txn boundary they get —
        # a cached replay must not hold the write lock for minutes
        self.conn.commit()
        content = archive.load(self.source, row["content_sha256"])
        try:
            text = content.decode(row["encoding"] or "utf-8", errors="replace")
        except LookupError:  # legacy pseudo-encodings in old rows
            text = content.decode("utf-8", errors="replace")
        return FetchResult(
            url,
            row["status_code"],
            content,
            text,
            row["encoding"] or "utf-8",
            row["content_sha256"],
            row["id"],
            from_cache=True,
        )

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body=None,
        params=None,
        headers=None,
        retries: int = 3,
    ) -> tuple[httpx.Response | None, Exception | None]:
        """Network only — no DB, no archive. Safe to call from worker threads."""
        host = httpx.URL(url).host
        last_exc: Exception | None = None
        for attempt in range(retries):
            self._throttle(host)
            try:
                resp = self.client.request(
                    method, url, json=json_body, params=params, headers=headers
                )
            except (httpx.HTTPError, ssl.SSLError) as e:
                # httpx.HTTPError also covers non-transport request failures
                # (TooManyRedirects, InvalidURL, …) that must not kill a
                # multi-thousand-page crawl; they surface as ConnectionError
                # after retries and are recorded as failed fetches
                last_exc = e
                time.sleep(2**attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                # 429 = API rate limit (api.data.gov quotas): back off in
                # minutes, not seconds, so overnight backfills ride it out
                time.sleep(min(300, 2**attempt * (60 if resp.status_code == 429 else 2)))
                continue
            return resp, None
        return None, last_exc

    def _record(self, resp: httpx.Response, method: str, body_repr: str | None) -> FetchResult:
        """Archive the body and insert the raw_fetches row. Calling thread only."""
        text, encoding = self._decode(resp)
        sha = archive.store(self.source, resp.content) if resp.content else None
        cur = self.conn.execute(
            "INSERT INTO raw_fetches (source, url, method, request_body, fetched_at, "
            "status_code, content_sha256, content_type, encoding, extraction_method) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.source,
                str(resp.url),
                method,
                body_repr,
                db.utcnow(),
                resp.status_code,
                sha,
                resp.headers.get("content-type"),
                encoding,
                self.extraction_method,
            ),
        )
        self.conn.commit()
        return FetchResult(
            str(resp.url),
            resp.status_code,
            resp.content,
            text,
            encoding,
            sha or "",
            cur.lastrowid,
        )

    def fetch_many(
        self,
        urls: list[str],
        *,
        concurrency: int = 6,
        headers=None,
        retries: int = 3,
        cache: bool = True,
    ):
        """Yield (url, FetchResult|None) for many GETs, N requests in flight.

        Bodies download concurrently; archive writes and `raw_fetches` inserts
        happen on the calling thread, because the sqlite3 connection is bound to
        it and the archive is not worth locking. `rate_per_host` still bounds the
        request *rate* (see _throttle) — concurrency only hides per-request
        latency, which is what dominates on multi-MB transcript bodies.
        """
        pending = []
        for url in urls:
            hit = self._cached(url) if cache else None
            if hit is not None:
                yield url, hit
            else:
                pending.append(url)
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {
                pool.submit(self._request, u, headers=headers, retries=retries): u for u in pending
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    resp, exc = fut.result()
                except Exception as e:  # defensive: _request should not raise
                    resp, exc = None, e
                if resp is None:
                    self._record_failure_soft(url, exc)
                    yield url, None
                else:
                    yield url, self._record(resp, "GET", None)

    def _record_failure_soft(
        self,
        url: str,
        exc: Exception | None,
        *,
        method: str = "GET",
        body_repr: str | None = None,
    ) -> None:
        """Record a dead fetch so coverage gaps stay visible. Never raises."""
        self.conn.execute(
            "INSERT INTO raw_fetches (source, url, method, request_body, fetched_at, "
            "status_code, extraction_method, note) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.source,
                url,
                method,
                body_repr,
                db.utcnow(),
                None,
                self.extraction_method,
                f"transport error: {exc}",
            ),
        )
        self.conn.commit()
