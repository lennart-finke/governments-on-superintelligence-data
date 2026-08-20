"""China full crawl: CAC, MFA (en+zh), gov.cn, MOST, MIIT, People's Daily.

Crawls are date-bounded: listing pages are walked newest→oldest and stop once a
whole page falls before the window. Discovery route differs by site:

  cn_mfa    — static index_N pagination reaches 2022 directly.
  cn_most   — same static index_N layout as MFA (t<YYYYMMDD> article URLs).
  cn_gov    — zh JSON feed (~1 month) + en page_N; deeper history via Wayback CDX.
  cn_cac    — only listing page 1 is static; history via Wayback CDX over the
              dated article-URL pattern.
  cn_miit   — listing pages are jpaas JS shells; the underlying unit API
              (front/page/build/unit + paramJson pageNo) is fetched directly
              and paginates to 2017. Dates come from the listing fragment,
              not the URL (MIIT URLs carry only the year).
  cn_people — People's Daily FRONT PAGE only (leadership readouts live there;
              inner pages are ~10× volume for little signal — deliberate
              bound). Live /pc/ layout pages exist from PC_CUTOVER; older
              editions 403 at origin, so discovery AND article fetches go
              through Wayback (old nw.*-01.htm URLs embed the date).

Articles become ONE utterance each (full text), so the adjudicator gets
whole-readout context; speakers are usually named only in the text (习近平强调…),
so speaker_raw is left NULL and the adjudicator extracts `speaker_name`. Xinhua
is skipped: its RSS is
dead (2017) and pagination is JS-only; gov.cn + People's Daily front page +
MFA mirror the leadership readouts it would contribute.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from ...http import Fetcher
from ..base import DocDate, Ingester
from .parse import extract_article

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"


def _date_ok(d: DocDate | None, start: date, end: date) -> bool:
    return d is not None and start <= d.date <= end


class CNCrawlIngester(Ingester):
    """Shared machinery: listing walk → dated article URLs → archive+parse."""

    jurisdiction = "CN"
    default_language = "zh"
    article_re: re.Pattern  # groups: url; date parsed by _url_date
    max_articles = 4000  # per-run LIVE-fetch valve (settings: max_articles)
    wayback_first = False  # True when the origin no longer serves old URLs at all

    def __init__(self, conn, settings=None):
        super().__init__(conn, settings)
        self._wb_ts: dict[str, str] = {}  # url -> snapshot timestamp (from CDX)
        self._wbf: Fetcher | None = None

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def _url_date(self, url: str) -> DocDate | None:
        """Date the source states for this article, with its precision.

        Returning DocDate rather than `date` is deliberate: a source that gives
        only a month must say so rather than quietly pick a day. See base.DocDate.
        """
        raise NotImplementedError

    def _listing_urls(self, f: Fetcher, start: date, end: date):
        """Yield article URLs from listing pages (site-specific)."""
        raise NotImplementedError

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "articles": 0,
            "skipped": 0,
            "failed": 0,
            "live_fetches": 0,
            "truncated": False,
        }
        valve = int(self.settings.get("max_articles", self.max_articles))
        # short timeouts matter here: CDX-discovered historical URLs often
        # hang at the origin rather than 404, and each hang costs timeout×retries
        timeout = float(self.settings.get("timeout", 120))
        rate = float(self.settings.get("rate_per_host", 1.0))
        seen: set[str] = set()
        with (
            Fetcher(
                self.conn,
                self.source,
                rate_per_host=rate,
                tolerant_tls=True,
                timeout=timeout,
            ) as f,
            Fetcher(self.conn, self.source, rate_per_host=rate, extraction_method="wayback") as wbf,
        ):
            self._wbf = wbf
            try:
                for url in self._listing_urls(f, start, end):
                    if url in seen:
                        continue
                    seen.add(url)
                    if not _date_ok(self._url_date(url), start, end):
                        stats["skipped"] += 1
                        continue
                    if stats["live_fetches"] >= valve:
                        # never silent: cli marks the window 'partial' so the next
                        # run resumes (archived articles no longer count here)
                        stats["truncated"] = True
                        break
                    ok, from_cache = self._ingest_article(f, url)
                    if ok:
                        stats["articles"] += 1
                        if not from_cache:
                            stats["live_fetches"] += 1
                    else:
                        stats["failed"] += 1
                        stats["live_fetches"] += 1
                    if stats["articles"] % 50 == 0:
                        # cached replays do no network fetches, so nothing else
                        # commits for us — never hold the write lock for long
                        self.conn.commit()
            finally:
                self._wbf = None
        self.conn.commit()
        return stats

    @staticmethod
    def _usable(res) -> bool:
        return res is not None and res.status_code == 200 and len(res.text) >= 400

    def _snapshot(self, url: str):
        """Fetch the CDX-recorded Wayback snapshot for a dead-at-origin URL."""
        ts = self._wb_ts.get(url)
        if ts is None or self._wbf is None:
            return None
        try:
            return self._wbf.fetch(f"https://web.archive.org/web/{ts}id_/{url}")
        except ConnectionError:
            return None

    def _fetch_article(self, f: Fetcher, url: str):
        """Live fetch with Wayback fallback for URLs discovered via CDX.

        wayback_first sources skip the doomed live attempt (origin 403s its
        whole archive); others try live first — origins often still serve old
        URLs even when their listings no longer reach them.
        """
        if self.wayback_first and url in self._wb_ts:
            return self._snapshot(url)
        try:
            res = f.fetch(url)
        except ConnectionError:
            res = None
        if not self._usable(res) and url in self._wb_ts:
            return self._snapshot(url)
        return res

    def _ingest_article(self, f: Fetcher, url: str) -> tuple[bool, bool]:
        """Returns (ok, from_cache) — cached re-ingests don't count against the valve."""
        res = self._fetch_article(f, url)
        if res is None or not self._usable(res):
            return False, False
        title, paras = extract_article(res.text)
        text = "\n".join(paras)
        if len(text) < 100:
            return False, res.from_cache
        native_id = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        lang = "en" if ("english" in url or "mfa_eng" in url) else "zh"
        dd = self._url_date(url)
        doc_id, _ = self.upsert_document(
            native_id,
            url=url,
            doc_date=dd.isoformat() if dd else None,
            date_precision=dd.precision if dd else "day",
            title=title,
            language=lang,
            doc_type="readout",
            content_for_hash=text,
            raw_fetch_id=res.raw_fetch_id,
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=None,
            language=lang,
            speech_context=title,
            is_verbatim=False,
        )
        return True, res.from_cache

    def _cdx_rows(
        self, f: Fetcher, pattern: str, start: date, end: date, fl: str = "original"
    ) -> list[list[str]]:
        """Wayback CDX index rows for a URL prefix (dedup by urlkey)."""
        try:
            res = f.fetch(
                WAYBACK_CDX,
                params={
                    "url": pattern,
                    "matchType": "prefix",
                    "output": "json",
                    "from": start.strftime("%Y"),
                    "to": end.strftime("%Y%m%d"),
                    "filter": "statuscode:200",
                    "collapse": "urlkey",
                    "fl": fl,
                },
                cache=False,
                retries=2,
            )
            rows = json.loads(res.text)
        except (ConnectionError, ValueError):
            return []
        return rows[1:]

    def _cdx_urls(self, f: Fetcher, pattern: str, start: date, end: date) -> list[str]:
        """Historical article URLs from the Wayback CDX index (dedup by urlkey)."""
        return [r[0] for r in self._cdx_rows(f, pattern, start, end)]


class CNMFAIngester(CNCrawlIngester):
    source = "cn_mfa"
    SECTIONS = [
        (
            "https://www.mfa.gov.cn/mfa_eng/xw/zyxw/",
            "index{}.html",
            re.compile(r'href="\./((\d{6})/t(\d{8})_\d+\.html)"'),
        ),
        (
            "https://www.mfa.gov.cn/mfa_eng/xw/zyjh/",
            "index{}.html",
            re.compile(r'href="\./((\d{6})/t(\d{8})_\d+\.html)"'),
        ),
        (
            "https://www.mfa.gov.cn/web/zyxw/",
            "index{}.shtml",
            re.compile(r'href="\./((\d{6})/t(\d{8})_\d+\.shtml)"'),
        ),
    ]

    # static pagination only retains ~2 years; older editions come from CDX
    HIST_PREFIXES = ["www.mfa.gov.cn/web/zyxw/", "www.mfa.gov.cn/mfa_eng/xw/"]

    def _url_date(self, url: str) -> DocDate | None:
        m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", url)
        return DocDate.of_day(*map(int, m.groups())) if m else None

    def _listing_urls(self, f, start, end):
        newest_reached = None
        for base, page_fmt, link_re in self.SECTIONS:
            page = 0
            while True:
                suffix = page_fmt.format("" if page == 0 else f"_{page}")
                try:
                    res = f.fetch(base + suffix, cache=False)
                except ConnectionError:
                    break
                if res.status_code != 200:
                    break
                links = link_re.findall(res.text)
                if not links:
                    break
                dates = []
                for rel, _, ymd in links:
                    url = base + rel
                    dates.append(date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])))
                    yield url
                if dates:
                    oldest = min(dates)
                    newest_reached = min(newest_reached or oldest, oldest)
                if dates and max(dates) < start:  # whole page pre-window: stop
                    break
                page += 1
        # pre-retention history: CDX-discovered dated URLs, snapshot fallback
        if newest_reached is None or newest_reached > start:
            for prefix in self.HIST_PREFIXES:
                for ts, original in self._cdx_rows(f, prefix, start, end, fl="timestamp,original"):
                    if self._url_date(original):
                        self._wb_ts[original] = ts
                        yield original


class CNGovIngester(CNCrawlIngester):
    source = "cn_gov"
    ZH_JSON = "https://www.gov.cn/yaowen/liebiao/YAOWENLIEBIAO.json"
    EN_BASE = "https://english.www.gov.cn/policies/latestreleases/"
    EN_RE = re.compile(
        r'href="(?:https?:)?(//english\.www\.gov\.cn/policies/[a-z]+/(\d{6})/(\d{2})/content_WS[0-9a-f]+\.html)"'
    )

    def _url_date(self, url: str) -> DocDate | None:
        m = re.search(r"/(\d{4})(\d{2})/(\d{2})/content_WS", url)
        if m:  # english URLs carry the day
            return DocDate.of_day(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"/(\d{4})(\d{2})/content_\d+\.htm", url)
        if m:
            # zh URLs carry only YYYYMM. The JSON feed has the day but covers
            # only the recent window, so backfilled articles have none here --
            # `tracker resolve-dates` recovers it from the archived body, which
            # states it in a <meta name="firstpublishedtime"> and in .pages-date.
            return DocDate.of_month(int(m.group(1)), int(m.group(2)))
        return None

    def _listing_urls(self, f, start, end):
        try:  # zh: JSON feed (~last month, incremental layer)
            res = f.fetch(self.ZH_JSON, cache=False)
            for item in json.loads(res.text.strip("﻿ \n")):
                url = item.get("URL", "")
                if url.startswith("http"):
                    yield url
        except (ConnectionError, ValueError):
            pass
        page = 1  # en: static page_N pagination
        while True:
            suffix = "" if page == 1 else f"page_{page}.html"
            try:
                res = f.fetch(self.EN_BASE + suffix, cache=False)
            except ConnectionError:
                break
            if res.status_code != 200:
                break
            links = self.EN_RE.findall(res.text)
            if not links:
                break
            newest = None
            for path, _, _ in links:
                yield "https:" + path
                dd = self._url_date(path)
                d = dd.date if dd else None
                newest = max(newest, d) if newest and d else (d or newest)
            if newest and newest < start:
                break
            page += 1
        # zh history beyond the JSON feed: Wayback CDX over the dated URL prefix
        yield from self._cdx_urls(f, "www.gov.cn/yaowen/liebiao/2", start, end)


class CNCACIngester(CNCrawlIngester):
    source = "cn_cac"
    LISTINGS = [
        "https://www.cac.gov.cn/yaowen/szyw/A093601index_1.htm",
        "https://www.cac.gov.cn/yaowen/wxyw/A093602index_1.htm",
    ]
    # hrefs are UNQUOTED on cac.gov.cn — regex, not attribute parsing
    LINK_RE = re.compile(
        r"href=(?:\"|')?(?:https?:)?(//www\.cac\.gov\.cn/(\d{4})-(\d{2})/(\d{2})/c_\d+\.htm)"
    )

    def _url_date(self, url: str) -> DocDate | None:
        m = re.search(r"/(\d{4})-(\d{2})/(\d{2})/c_", url)
        return DocDate.of_day(*map(int, m.groups())) if m else None

    def _listing_urls(self, f, start, end):
        for listing in self.LISTINGS:  # live page 1: incremental layer (20 items)
            try:
                res = f.fetch(listing, cache=False)
            except ConnectionError:
                continue
            if res.status_code == 200:
                for path, *_ in self.LINK_RE.findall(res.text):
                    yield "https:" + path
        # history: CDX over the dated article prefix (/YYYY-MM/DD/c_<id>.htm)
        yield from self._cdx_urls(f, "www.cac.gov.cn/20", start, end)


class CNMOSTIngester(CNMFAIngester):
    """MOST press room: same static index_N layout and t<YYYYMMDD> article URLs as MFA."""

    source = "cn_most"
    SECTIONS = [
        (
            "https://www.most.gov.cn/kjbgz/",
            "index{}.html",  # 科技部工作动态
            re.compile(r'href="\./((\d{6})/t(\d{8})_\d+\.html)"'),
        ),
    ]
    HIST_PREFIXES = ["www.most.gov.cn/kjbgz/"]


class CNMIITIngester(CNCrawlIngester):
    """MIIT news via the jpaas unit API (the HTML listing is a JS shell).

    The column shell page embeds a queryData blob; the same GET the site's own
    unitbuild.js/page.js make returns HTML fragments with per-item dates, and
    paramJson={"pageNo":N} pages back to 2017. Article URLs carry only the
    publication YEAR, so dates are taken from the listing fragment.
    """

    source = "cn_miit"
    COLUMN = "https://www.miit.gov.cn/xwdt/gxdt/ldhd/index.html"  # 领导活动
    UNIT_API = "https://www.miit.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit"
    QUERYDATA_RE = re.compile(r'queryData="(\{[^"]+\})"')
    ITEM_RE = re.compile(
        r'href="(/[^"]+?/art/\d{4}/art_[0-9a-f]+\.html)"[^>]*>.*?'
        r'<span class="fr">(\d{4})-(\d{2})-(\d{2})</span>',
        re.S,
    )
    MAX_PAGES = 400  # 24 items/page; well past the 2691-item column depth

    def __init__(self, conn, settings=None):
        super().__init__(conn, settings)
        self._item_dates: dict[str, DocDate] = {}

    def _url_date(self, url: str) -> DocDate | None:
        return self._item_dates.get(url)

    def _listing_urls(self, f, start, end):
        try:
            shell = f.fetch(self.COLUMN, cache=False)
        except ConnectionError:
            return
        m = self.QUERYDATA_RE.search(shell.text)
        if not m:
            return
        query = json.loads(m.group(1).replace("'", '"'))
        query["editType"] = query.get("editType", "null")
        for page in range(1, self.MAX_PAGES + 1):
            params = dict(query, paramJson=json.dumps({"pageNo": page, "pageSize": "24"}))
            try:
                res = f.fetch(
                    self.UNIT_API,
                    params=params,
                    cache=False,
                    headers={"Referer": self.COLUMN},
                )
                html = json.loads(res.text)["data"]["html"]
            except (ConnectionError, ValueError, KeyError, TypeError):
                break
            items = self.ITEM_RE.findall(html)
            if not items:
                break
            dates = []
            for path, y, mth, dd in items:
                url = "https://www.miit.gov.cn" + path
                d = date(int(y), int(mth), int(dd))
                self._item_dates[url] = DocDate(d, "day")
                dates.append(d)
                yield url
            if max(dates) < start:  # whole page pre-window: stop
                break


class CNPeopleIngester(CNCrawlIngester):
    """People's Daily (人民日报) FRONT PAGE — the page carrying leadership readouts.

    Live /pc/ layout pages exist from PC_CUTOVER onward; earlier editions 403
    at origin (CDN), so pre-cutover discovery uses Wayback CDX over the old
    layout (nw.D110000renmrb_<YYYYMMDD>_<n>-01.htm = article n on page 01) and
    article bodies are fetched from Wayback snapshots. Windowed like ordinary
    sources (window_days chunks), not as one whole-history crawl.
    """

    source = "cn_people"
    PC_CUTOVER = date(2024, 12, 1)  # /pc/ layout 404s before this
    PC_BASE = "http://paper.people.com.cn/rmrb/pc/"
    PC_ARTICLE_RE = re.compile(r'href="(?:\.\./)*(content/(\d{6})/(\d{2})/content_\d+\.html)"')
    OLD_PREFIX = "paper.people.com.cn/rmrb/html/"
    OLD_FRONT_RE = re.compile(r"/nw\.D110000renmrb_(\d{8})_\d+-01\.htm$")
    wayback_first = True  # origin 403s its entire pre-cutover archive

    def windows(self, start=None, end=None):
        return Ingester.windows(self, start, end)  # chunked, not whole-history

    def _url_date(self, url: str) -> DocDate | None:
        m = self.OLD_FRONT_RE.search(url)
        if m:
            s = m.group(1)
            return DocDate.of_day(int(s[:4]), int(s[4:6]), int(s[6:8]))
        m = re.search(r"/pc/content/(\d{4})(\d{2})/(\d{2})/content_", url)
        return DocDate.of_day(*map(int, m.groups())) if m else None

    def _listing_urls(self, f, start, end):
        day = max(start, self.PC_CUTOVER)
        while day <= end:  # live layer: one front-page layout fetch per day
            ym, dd = day.strftime("%Y%m"), day.strftime("%d")
            try:
                res = f.fetch(f"{self.PC_BASE}layout/{ym}/{dd}/node_01.html", cache=False)
            except ConnectionError:
                res = None
            if res is not None and res.status_code == 200:
                for rel, *_ in self.PC_ARTICLE_RE.findall(res.text):
                    yield self.PC_BASE + rel
            day += timedelta(days=1)
        if start < self.PC_CUTOVER:  # history: Wayback CDX per month, front page only
            month = date(start.year, start.month, 1)
            hist_end = min(end, self.PC_CUTOVER)
            while month < hist_end:
                for ts, original in self._cdx_rows(
                    f,
                    f"{self.OLD_PREFIX}{month:%Y-%m}/",
                    start,
                    end,
                    fl="timestamp,original",
                ):
                    if self.OLD_FRONT_RE.search(original):
                        self._wb_ts[original] = ts
                        yield original
                month = (
                    date(month.year + 1, 1, 1)
                    if month.month == 12
                    else date(month.year, month.month + 1, 1)
                )
