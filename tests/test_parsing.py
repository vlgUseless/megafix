import pytest

from megafix.interfaces.cli_parsing import parse_issue_url, parse_pr_url


def test_parse_issue_url_ok():
    assert parse_issue_url("https://github.com/foo/bar/issues/123") == (
        "foo",
        "bar",
        123,
    )


def test_parse_pr_url_ok():
    assert parse_pr_url("https://github.com/foo/bar/pull/7") == ("foo", "bar", 7)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/foo/bar/issue/123",
        "https://github.com/foo/bar/pulls/7",
        "not a url",
    ],
)
def test_parse_bad_urls(url):
    with pytest.raises(ValueError):
        if "pull" in url:
            parse_pr_url(url)
        else:
            parse_issue_url(url)
