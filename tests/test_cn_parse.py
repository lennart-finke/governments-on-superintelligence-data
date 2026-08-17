from tracker.ingest.cn.parse import extract_article


def test_extract_xi_politburo_article(fixtures):
    title, paras = extract_article(
        (fixtures / "cn_xi_politburo.html").read_text(encoding="utf-8")
    )
    assert title and "Xi" in title and len(paras) >= 5
    assert "risks and challenges not seen before" in " ".join(paras)
