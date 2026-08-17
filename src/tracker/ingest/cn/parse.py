"""Shared HTML → article-text extraction for Chinese government sites.

These sites have no semantic markup consistency; we extract the densest text
container and split into paragraph utterances. Charset quirks are handled
upstream in http.Fetcher (charset-normalizer fallback for GB2312/GBK).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# containers that commonly hold the article body across gov.cn / MFA / CAC / MIIT
BODY_SELECTORS = [
    "div#UCAP-CONTENT",  # www.gov.cn
    "div.pages_content",  # gov.cn variants
    "div#News_Body_Txt_A",  # mfa older
    "div.article-con",
    "div#detailContent",
    "div.TRS_Editor",  # CAC / MIIT (TRS CMS)
    "div.trs_editor_view",  # MOST (TRS CMS, newer skin)
    "div#con_con",  # MIIT article body (jpaas CMS)
    "div#ozoom",  # people's daily (paper.people.com.cn)
    "div#zoom",  # xinhua / people's daily older
    "div.article",
    "div#Content",
    "div.content",
]


def extract_article(html: str) -> tuple[str | None, list[str]]:
    """Return (title, paragraphs)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)

    body = None
    for sel in BODY_SELECTORS:
        body = soup.select_one(sel)
        if body and len(body.get_text(strip=True)) > 200:
            break
        body = None
    if body is None:
        # fallback: densest <div> by text length
        candidates = sorted(
            soup.find_all("div"),
            key=lambda d: len(d.get_text(strip=True)),
            reverse=True,
        )
        body = candidates[0] if candidates else soup

    paras = []
    for p in body.find_all("p") or [body]:
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        # Chinese text has no spaces; keep fullwidth spacing intact
        text = text.strip()
        if len(text) >= 20:
            paras.append(text)
    if not paras:
        text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
        paras = [text] if text else []
    return title, paras
