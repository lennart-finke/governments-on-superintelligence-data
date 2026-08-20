from datetime import date

from lxml import etree

from tracker.ingest.base import DocDate
from tracker.ingest.cn.crawl import CNCACIngester, CNGovIngester, CNMFAIngester
from tracker.ingest.de_bundestag import DEBundestagIngester
from tracker.ingest.fr_senat import CRI_NS, FRSenatIngester

REDE = etree.fromstring("""
<rede id="ID1">
  <p klasse="redner"><redner id="11001"><name><vorname>Anna</vorname>
    <nachname>Beispiel</nachname><fraktion>SPD</fraktion></name></redner>Anna Beispiel (SPD):</p>
  <p klasse="J_1">Erster Satz zur Superintelligenz.</p>
  <kommentar>(Beifall)</kommentar>
  <name>Präsidentin X:</name>
  <p klasse="J">Zwischenruf der Präsidentin — nicht Anna.</p>
  <p klasse="redner"><redner id="11001"><name><vorname>Anna</vorname>
    <nachname>Beispiel</nachname></name></redner></p>
  <p klasse="O">Fortsetzung von Anna.</p>
</rede>""")


def test_bundestag_rede_parse():
    speaker, native_id, text = DEBundestagIngester._parse_rede(REDE)
    assert (speaker, native_id) == ("Anna Beispiel (SPD)", "11001")
    assert "Superintelligenz" in text and "Fortsetzung" in text
    assert "nicht Anna" not in text  # chair interjection dropped


def test_senat_intervenant_parse(conn):
    # default XHTML xmlns mirrors the real cri.zip files: unprefixed <p>/<span>
    # are namespace-qualified there (a bare-tag parser matches nothing)
    xml = f"""<cri:cri xmlns:cri="{CRI_NS}" xmlns="http://www.w3.org/1999/xhtml">
      <div class="intervenant">
      <cri:intervenant id="i1" mat="19591F" nom="Xavier IACOVELLI" civ="M." qua="pr" type="1">
        <p id="p1"><span class="orateur_nom"><cri:orateurnom>M. X.</cri:orateurnom></span>
           La singularite approche.</p>
        <p id="p2"><span class="info_entre_parentheses">
           <cri:infoentreparentheses>(Rires)</cri:infoentreparentheses></span></p>
      </cri:intervenant></div></cri:cri>""".encode("ISO-8859-1")
    ing = FRSenatIngester(conn, settings={})
    assert ing._ingest_sitting(xml, date(2026, 1, 5), raw_fetch_id=None) == 1
    u = conn.execute("SELECT * FROM utterances").fetchone()
    assert u["speaker_raw"] == "M. Xavier IACOVELLI (pr)" and u["speaker_native_id"] == "19591F"
    assert "singularite" in u["text"] and "Rires" not in u["text"] and "M. X." not in u["text"]


def test_cn_url_dates():
    assert CNCACIngester._url_date(None, "https://www.cac.gov.cn/2026-07/13/c_1.htm") == DocDate(
        date(2026, 7, 13), "day"
    )
    assert CNMFAIngester._url_date(None, ".../202607/t20260713_11980494.html") == DocDate(
        date(2026, 7, 13), "day"
    )
    assert CNGovIngester._url_date(
        None,
        "//english.www.gov.cn/policies/latestreleases/202607/13/content_WSabc.html",
    ) == DocDate(date(2026, 7, 13), "day")
    assert CNMFAIngester._url_date(None, "https://x/nodate.html") is None


def test_cn_gov_zh_url_is_month_precision_not_a_guessed_day():
    """A gov.cn zh article URL carries no day, and must not pretend otherwise.

    This is the regression that put 72 published quotes on the first of a month,
    wrong by up to five weeks and indistinguishable from a real date. The day in
    `.date` is a placeholder for ordering; `.precision` is what says so.
    """
    got = CNGovIngester._url_date(None, "https://www.gov.cn/yaowen/liebiao/202607/content_1.htm")
    assert got == DocDate(date(2026, 7, 1), "month")
    assert got.precision == "month"
