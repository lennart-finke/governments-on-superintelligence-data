from tracker.ingest.ec_presscorner import ECPresscornerIngester

_html_to_text = ECPresscornerIngester._html_to_text


def test_inline_spans_do_not_split_words():
    # Real presscorner markup wraps stray punctuation in inline RTL spans
    # (SPEECH/26/302, Kubilius). The naive tags->space flatten produced
    # "don ' t" and `" killer robots"`.
    html = (
        "<p>First: We are the &ldquo;good guys&rdquo;. "
        'We don<span dir="RTL">\'</span>t fund weapons forbidden by '
        'international law. Or <span dir="RTL">&ldquo;</span>killer robots&rdquo; '
        "without human oversight.</p>"
    )
    text = _html_to_text(html)
    assert "don't fund weapons" in text
    assert "“killer robots”" in text
    assert "don ' t" not in text
    assert "“ killer robots" not in text


def test_block_tags_still_separate():
    html = "<p>There are limits.</p><p>First point.</p>"
    assert _html_to_text(html) == "There are limits. First point."


def test_br_separates():
    assert _html_to_text("line one<br>line two") == "line one line two"
