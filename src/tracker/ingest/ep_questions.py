"""European Parliament parliamentary questions — and the answers to them.

Open Data API v2, same service as ep_plenary:
  /parliamentary-questions?year=Y&limit=1000&offset=N  → identifiers for a year
  /parliamentary-questions/{id,id,…}                   → up to 30 records a call:
      author person IDs, original language, titles in every language it exists
      in, the DOCX/PDF manifestations, and `inverse_answers_to` — the answer
      document, nested whole, so an answer costs no extra API call
  distribution/reds_iMaQp/{id}/{id}_{lang}.docx        → the question text
  distribution/reds_iMaQp_Asw/{id}-ASW/…_{lang}.docx   → the answer text
      (both 302 to redmapl3.europarl.europa.eu)

The answers are half the point. A written question is an MEP asking; the answer
is the Commission (occasionally the Council, the ECB or the EEAS) stating an
official position on the record, signed by the responsible Commissioner —
"Answer given by Mr Dombrovskis on behalf of the European Commission". Those
land here as their own documents, attributed to the Commissioner who signed
them, so a quote from one is not attributed to the MEP who asked.

Volume runs to a few thousand questions a year, overwhelmingly written ones with
a scattering of oral questions and major interpellations, plus roughly one answer
each. The API ceiling is a documented 500 requests / 5 min, and enumeration is
cheap (batched 30 ids per call); the DOCX bodies dominate, ~10 kB each.

Text is stored in the language the question was tabled in, per the project's
native-language rule. Answers are stored in the language the API reports as
theirs, English when it reports none.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date

from lxml import etree

from ..http import Fetcher
from .base import Ingester

API = "https://data.europarl.europa.eu/api/v2"
DIST = "https://data.europarl.europa.eu/"
DOCEO = "https://www.europarl.europa.eu/doceo/document/{id}_EN.html"
# the /{doc-id} endpoint takes a comma-separated list; 30 is comfortably served
BATCH = 30
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# "Answer given by Mr Dombrovskis on behalf of the European Commission"
ANSWERED_BY = re.compile(
    r"Answer given by\s+(.{2,80}?)\s+on behalf of\s+(?:the\s+)?([^(\n]{3,60})",
    re.IGNORECASE,
)
QUESTION_TYPES = {
    "QUESTION_WRITTEN": "question_written",
    "QUESTION_WRITTEN_PRIORITY": "question_written_priority",
    "QUESTION_ORAL": "question_oral",
    "INTERPELLATION_MAJOR": "interpellation_major",
}


def docx_text(content: bytes) -> str:
    """Flatten a .docx to text, one line per paragraph.

    Questions and answers are short, single-section documents, so paragraphs are
    all the structure there is to keep.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        return ""
    try:
        root = etree.fromstring(xml, etree.XMLParser(recover=True, resolve_entities=False))
    except (etree.XMLSyntaxError, ValueError):
        return ""
    if root is None:
        return ""
    lines = []
    for para in root.iter(f"{W}p"):
        # runs must be walked in document order, not just the <w:t> text: the
        # header of an answer is one paragraph of runs separated by <w:cr/>
        # ("EN" / "E-000001/2025" / "Answer given by Mr Dombrovskis" / "on
        # behalf of the European Commission"), and joining only the text nodes
        # welds them into "Mr Dombrovskison behalf of".
        parts = []
        for node in para.iter():
            tag = node.tag
            if tag == f"{W}t":
                parts.append(node.text or "")
            elif tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
            elif tag == f"{W}tab":
                parts.append(" ")
        for line in "".join(parts).split("\n"):
            line = re.sub(r"[ \t]+", " ", line).strip()
            if line:
                lines.append(line)
    return "\n".join(lines)


def _tail(v) -> str:
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("id") or v.get("@id") or ""
    return str(v).rsplit("/", 1)[-1]


def _lang_of(expression_id: str) -> str:
    """'eli/dl/doc/E-10-2025-000001/en' -> 'en'."""
    return expression_id.rstrip("/").rsplit("/", 1)[-1].lower()


def expressions(record: dict) -> dict[str, dict]:
    """language -> the expression (per-language titles + manifestations)."""
    out: dict[str, dict] = {}
    for expr in record.get("is_realized_by") or []:
        lang = _lang_of(str(expr.get("id") or ""))
        if lang and len(lang) <= 3 and isinstance(expr, dict):
            out[lang] = expr
    return out


def docx_path(expr: dict) -> str | None:
    for man in expr.get("is_embodied_by") or []:
        path = man.get("is_exemplified_by") or ""
        if str(man.get("id", "")).endswith("docx") and path:
            return str(path)
    return None


def manifestations(record: dict) -> dict[str, str]:
    """language -> DOCX path, for every language the work exists in."""
    return {lang: path for lang, expr in expressions(record).items() if (path := docx_path(expr))}


def original_language(record: dict) -> str | None:
    codes = record.get("originalLanguage") or []
    if isinstance(codes, str):
        codes = [codes]
    if not codes:
        return None
    three = _tail(codes[0]).upper()
    from .ep_plenary import LANG3TO2

    return LANG3TO2.get(three) or (three.lower()[:2] or None)


def pick_language(mans: dict[str, str], preferred: str | None) -> str | None:
    for lang in (preferred, "en"):
        if lang and lang in mans:
            return lang
    return next(iter(mans), None)


def title_of(record: dict, lang: str | None) -> str | None:
    titles = record.get("title_dcterms") or {}
    if not isinstance(titles, dict):
        return None
    if lang and titles.get(lang):
        return titles[lang]
    return titles.get("en") or next(iter(titles.values()), None)


def authors_from_title(record: dict, lang: str | None) -> str | None:
    """Author names off the long-form title.

    `title_alternative` reads "Question for written answer E-000001/2025 - to
    the Commission - Rule 144 - Anna Bryłka (PfE)": the tabling MEPs are the
    last hyphen-separated field. Note it hangs off the *expression*, not the
    work, so it is per-language and absent from the work's own fields. The
    structured `creator` gives person IDs but no names, and resolving each
    against /meps would add a request per MEP for something already here.
    """
    exprs = expressions(record)
    expr = (exprs.get(lang) if lang else None) or exprs.get("en") or next(iter(exprs.values()), {})
    alt = expr.get("title_alternative") or {}
    if not isinstance(alt, dict):
        return None
    text = (alt.get(lang) if lang else None) or alt.get("en") or next(iter(alt.values()), None)
    if not text:
        return None
    tail = str(text).split(" - ")[-1].strip()
    # a bare rule reference, or no separator at all, means the pattern held only
    # for the prefix and there are no names to take
    if (
        not tail
        or tail == str(text).strip()
        or re.fullmatch(r"(?i)(rule|art\.?|article|artikel|règle)\s*[\d.]+", tail)
    ):
        return None
    return tail[:200]


def authors_from_text(text: str) -> str | None:
    """Author names off the question's own header, for records without a title.

    Term-9 questions often carry no `title_alternative`, but the DOCX header is
    laid out the same in every language: reference, addressee, rule, authors,
    then a labelled subject line ("Subject:", "Przedmiot:", "Objet:"). So the
    authors are the line before the first label. Requiring the political group
    in brackets keeps this from picking up a stray header line.
    """
    lines = [ln.strip() for ln in text.split("\n")[:12] if ln.strip()]
    for i, line in enumerate(lines):
        if i and re.match(r"^[^\s:]{3,20}:\s", line):
            candidate = lines[i - 1]
            if "(" in candidate and ")" in candidate and re.search(r"\w", candidate):
                return candidate[:200]
            return None
    return None


class EPQuestionsIngester(Ingester):
    source = "ep_questions"
    jurisdiction = "EU"
    default_language = "mul"

    def windows(self, start: date | None = None, end: date | None = None):
        """Calendar-year windows: the listing endpoint filters by `year` only."""
        start = start or self.backfill_start
        end = end or date.today()
        covered = {
            (row["window_start"], row["window_end"])
            for row in self.conn.execute(
                "SELECT window_start, window_end FROM watermarks "
                "WHERE source=? AND status='done'",
                (self.source,),
            )
        }
        out = []
        for year in range(start.year, end.year + 1):
            w_start = max(start, date(year, 1, 1))
            w_end = min(end, date(year, 12, 31))
            if (w_start.isoformat(), w_end.isoformat()) not in covered:
                out.append((w_start, w_end))
        return out

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "listed": 0,
            "questions": 0,
            "answers": 0,
            "already_ingested": 0,
            "no_text": 0,
            "failed": 0,
        }
        headers = {"Accept": "application/ld+json"}
        rate = float(self.settings.get("rate_per_host", 1.5))
        with Fetcher(self.conn, self.source, rate_per_host=rate, timeout=180) as f:
            ids = []
            for year in range(start.year, end.year + 1):
                ids += self._year_ids(f, year, headers)
            stats["listed"] = len(ids)
            todo = [i for i in ids if not self._have(i)]
            stats["already_ingested"] = len(ids) - len(todo)
            for batch in (todo[i : i + BATCH] for i in range(0, len(todo), BATCH)):
                for record in self._records(f, batch, headers):
                    doc_date = str(record.get("document_date") or "")[:10]
                    if doc_date and not (start.isoformat() <= doc_date <= end.isoformat()):
                        continue
                    try:
                        n_q, n_a = self._ingest_question(f, record)
                    except ConnectionError:
                        stats["failed"] += 1
                        continue
                    stats["questions"] += n_q
                    stats["answers"] += n_a
                    if not n_q:
                        stats["no_text"] += 1
                # one commit per batch: a long backfill must not hold the write
                # lock, and a killed run resumes from the last batch
                self.conn.commit()
        self.conn.commit()
        return stats

    # -- listing ---------------------------------------------------------------

    def _year_ids(self, f: Fetcher, year: int, headers) -> list[str]:
        ids: list[str] = []
        offset = 0
        while True:
            url = f"{API}/parliamentary-questions?year={year}&offset={offset}&limit=1000"
            try:
                res = f.fetch(url, headers=headers, cache=False)
            except ConnectionError:
                break
            if res.status_code != 200 or not res.text.strip():
                break
            try:
                data = json.loads(res.text).get("data", [])
            except ValueError:
                break
            ids += [str(d["identifier"]) for d in data if d.get("identifier")]
            if len(data) < 1000:
                break
            offset += 1000
        return ids

    def _records(self, f: Fetcher, batch: list[str], headers) -> list[dict]:
        if not batch:
            return []
        try:
            res = f.fetch(
                f"{API}/parliamentary-questions/{','.join(batch)}",
                headers=headers,
                cache=False,
            )
        except ConnectionError:
            return []
        if res.status_code != 200 or not res.text.strip():
            return []
        try:
            return json.loads(res.text).get("data", [])
        except ValueError:
            return []

    def _have(self, native_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE source=? AND native_id=? LIMIT 1",
                (self.source, native_id),
            ).fetchone()
            is not None
        )

    # -- documents -------------------------------------------------------------

    def _ingest_question(self, f: Fetcher, record: dict) -> tuple[int, int]:
        """Store one question and every answer to it. Returns (questions, answers)."""
        native_id = str(record.get("identifier") or "")
        if not native_id:
            return 0, 0
        mans = manifestations(record)
        orig = original_language(record)
        lang = pick_language(mans, orig)
        text = self._body(f, mans.get(lang or "", ""))
        if not text:
            return 0, 0
        work_type = _tail(record.get("work_type") or "")
        authors = [_tail(c) for c in (record.get("creator") or [])]
        doc_id, _ = self.upsert_document(
            native_id,
            url=DOCEO.format(id=native_id),
            doc_date=str(record.get("document_date") or "")[:10] or None,
            title=title_of(record, lang),
            language=lang,
            doc_type=QUESTION_TYPES.get(work_type, "question"),
            content_for_hash=text,
            meta={
                "work_type": work_type,
                "authors": authors or None,
                "ep_number": record.get("epNumber"),
                "original_language": orig,
                "role": "question",
            },
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=authors_from_title(record, lang) or authors_from_text(text),
            speaker_native_id=authors[0] if authors else None,
            language=lang,
            speech_context=title_of(record, lang),
        )
        answers = 0
        for answer in record.get("inverse_answers_to") or []:
            answers += self._ingest_answer(f, answer, native_id)
        return 1, answers

    def _ingest_answer(self, f: Fetcher, answer: dict, question_id: str) -> int:
        native_id = str(answer.get("identifier") or _tail(answer.get("id") or ""))
        if not native_id:
            return 0
        mans = manifestations(answer)
        # answers are drafted in English and translated; the question's language
        # is not the answer's, so ask the record and fall back to English
        lang = pick_language(mans, original_language(answer) or "en")
        text = self._body(f, mans.get(lang or "", ""))
        if not text:
            return 0
        m = ANSWERED_BY.search(text)
        speaker = m.group(1).strip() if m else None
        institution = m.group(2).strip().rstrip(".,") if m else None
        doc_id, _ = self.upsert_document(
            native_id,
            url=DOCEO.format(id=native_id),
            doc_date=str(answer.get("document_date") or "")[:10] or None,
            title=title_of(answer, lang),
            language=lang,
            doc_type="question_answer",
            content_for_hash=text,
            meta={
                "answers_to": question_id,
                "role": "answer",
                "institution": institution or _tail(answer.get("creator") or "") or None,
            },
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=speaker,
            language=lang,
            speech_context=(
                f"Answer on behalf of {institution}"
                if institution
                else "Answer to a parliamentary question"
            ),
        )
        return 1

    def _body(self, f: Fetcher, path: str) -> str:
        """Download one DOCX manifestation and flatten it to text."""
        if not path:
            return ""
        res = f.fetch(DIST + str(path).lstrip("/"))
        if res.status_code != 200 or not res.content:
            return ""
        return docx_text(res.content)
