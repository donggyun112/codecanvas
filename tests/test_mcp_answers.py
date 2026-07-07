from codecanvas_mcp.mcp.answers import capped, DEFAULT_CAP


def test_capped_under_limit_no_note():
    items, note = capped([1, 2, 3], cap=10)
    assert items == [1, 2, 3]
    assert note is None


def test_capped_over_limit_truncates_with_note():
    items, note = capped(list(range(10)), cap=4)
    assert items == [0, 1, 2, 3]
    assert note == "… 6 more (truncated)"


def test_capped_default_cap():
    items, note = capped(list(range(DEFAULT_CAP + 5)))
    assert len(items) == DEFAULT_CAP
    assert note == "… 5 more (truncated)"
