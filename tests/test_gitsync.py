import io
import json
import urllib.error

import pytest

from indexer import gitsync


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def _fake_urlopen(outcomes, calls):
    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        o = outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return _Resp(json.dumps(o).encode())
    return urlopen


@pytest.fixture
def sleeps(monkeypatch):
    s = []
    monkeypatch.setattr(gitsync.time, "sleep", s.append)
    return s


def test_get_json_retries_transient_errors(monkeypatch, sleeps):
    calls = []
    outcomes = [_http_error(504), urllib.error.URLError("reset"),
                TimeoutError(), _http_error(429), {"ok": 1}]
    monkeypatch.setattr(gitsync.urllib.request, "urlopen",
                        _fake_urlopen(outcomes, calls))
    assert gitsync.get_json("https://x") == {"ok": 1}
    assert len(calls) == 5
    assert sleeps == [1, 2, 4, 8]


def test_get_json_gives_up_after_attempts(monkeypatch, sleeps):
    calls = []
    outcomes = [_http_error(502)] * gitsync.API_ATTEMPTS
    monkeypatch.setattr(gitsync.urllib.request, "urlopen",
                        _fake_urlopen(outcomes, calls))
    with pytest.raises(urllib.error.HTTPError) as ei:
        gitsync.get_json("https://x")
    assert ei.value.code == 502
    assert len(calls) == gitsync.API_ATTEMPTS


def test_get_json_does_not_retry_4xx(monkeypatch, sleeps):
    calls = []
    monkeypatch.setattr(gitsync.urllib.request, "urlopen",
                        _fake_urlopen([_http_error(404)], calls))
    with pytest.raises(urllib.error.HTTPError):
        gitsync.get_json("https://x")
    assert len(calls) == 1 and sleeps == []


def test_list_public_repos_survives_504(monkeypatch, sleeps):
    calls = []
    page1 = [{"name": "b", "size": 1}, {"name": "a", "size": 1},
             {"name": "f", "size": 1, "fork": True}]
    outcomes = [_http_error(504), page1, []]
    monkeypatch.setattr(gitsync.urllib.request, "urlopen",
                        _fake_urlopen(outcomes, calls))
    assert gitsync.list_public_repos("org") == ["a", "b"]
    assert len(calls) == 3
