from datetime import date

from tracker.ingest.cn.crawl import CNMIITIngester, CNMOSTIngester, CNPeopleIngester

MIIT_FRAGMENT = """
<div id="右侧内容"><div class="page-content"><ul>
  <li class="cf">
    <a class="fl" href="/xwfb/bldhd/art/2026/art_0bd2e5ee157f46d4b97a449c235f3747.html"
       target="_blank" title="工业和信息化部召开座谈会"><i></i>工业和信息化部召开座谈会</a>
    <span class="fr">2026-07-10</span>
  </li>
  <li class="cf">
    <a class="fl" href="/xwfb/bldhd/art/2026/art_c28b8a77ef9b4d2d945d3f6a27a77b10.html"
       target="_blank" title="研讨班"><i></i>研讨班</a>
    <span class="fr">2026-07-08</span>
  </li>
</ul></div></div>
"""

RMRB_LAYOUT = """
<div class="news"><ul>
  <li><a href="../../../content/202607/12/content_30168132.html">头版头条</a></li>
  <li><a href="../../../content/202607/12/content_30168133.html">要闻</a></li>
</ul></div>
"""


def test_most_url_date():
    assert CNMOSTIngester._url_date(
        None, "https://www.most.gov.cn/kjbgz/202607/t20260710_197036.html"
    ) == date(2026, 7, 10)


def test_miit_item_re_pairs_href_and_date():
    items = CNMIITIngester.ITEM_RE.findall(MIIT_FRAGMENT)
    assert [(p, f"{y}-{m}-{d}") for p, y, m, d in items] == [
        (
            "/xwfb/bldhd/art/2026/art_0bd2e5ee157f46d4b97a449c235f3747.html",
            "2026-07-10",
        ),
        (
            "/xwfb/bldhd/art/2026/art_c28b8a77ef9b4d2d945d3f6a27a77b10.html",
            "2026-07-08",
        ),
    ]


def test_miit_querydata_parse():
    shell = (
        "<script id=\"x\" queryData=\"{'parseType':'buildstatic',"
        "'webId':'8d82','tagId': '右侧内容'}\"></script>"
    )
    m = CNMIITIngester.QUERYDATA_RE.search(shell)
    assert m is not None
    import json

    q = json.loads(m.group(1).replace("'", '"'))
    assert q["webId"] == "8d82" and q["tagId"] == "右侧内容"


def test_people_url_dates(conn):
    ing = CNPeopleIngester(conn, settings={})
    old = "http://paper.people.com.cn/rmrb/html/2022-03/15/" "nw.D110000renmrb_20220315_2-01.htm"
    new = "http://paper.people.com.cn/rmrb/pc/content/202607/12/content_30168132.html"
    inner_page = (
        "http://paper.people.com.cn/rmrb/html/2022-03/15/" "nw.D110000renmrb_20220315_2-04.htm"
    )
    assert ing._url_date(old) == date(2022, 3, 15)
    assert ing._url_date(new) == date(2026, 7, 12)
    # inner pages parse via OLD_FRONT_RE only for page 01; -04 is not front page
    assert CNPeopleIngester.OLD_FRONT_RE.search(inner_page) is None


def test_people_layout_article_re():
    rels = [r for r, *_ in CNPeopleIngester.PC_ARTICLE_RE.findall(RMRB_LAYOUT)]
    assert rels == [
        "content/202607/12/content_30168132.html",
        "content/202607/12/content_30168133.html",
    ]
