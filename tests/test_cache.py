"""Generic on-disk Fetcher cache (postmortem.cache)."""

from pathlib import Path

from postmortem.cache import (
    DEFAULT_TTL_SECONDS,
    ENV_VAR,
    cache_dir,
    cached_fetcher,
)


class TestCacheDir:
    def test_default_is_under_home_cache(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert cache_dir() == Path.home() / ".cache" / "postmortem"

    def test_env_var_overrides_and_is_a_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "somewhere"))
        assert cache_dir() == tmp_path / "somewhere"


class TestCachedFetcher:
    def test_hit_calls_underlying_fetcher_once(self, tmp_path):
        calls = []

        def fake(url):
            calls.append(url)
            return {"ok": True, "seen": len(calls)}

        fetcher = cached_fetcher(fake, cache_dir=tmp_path)
        assert fetcher("http://example/x") == {"ok": True, "seen": 1}
        assert fetcher("http://example/x") == {"ok": True, "seen": 1}  # cached
        assert len(calls) == 1

    def test_cache_persists_across_separate_fetcher_instances(self, tmp_path):
        """Simulates two separate CLI invocations sharing a cache dir."""
        calls = []

        def fake(url):
            calls.append(url)
            return {"ok": True}

        cached_fetcher(fake, cache_dir=tmp_path)("http://example/x")
        cached_fetcher(fake, cache_dir=tmp_path)("http://example/x")
        assert len(calls) == 1
        assert (tmp_path / "raiderio.json").exists()

    def test_different_urls_are_independent_entries(self, tmp_path):
        calls = []

        def fake(url):
            calls.append(url)
            return {"url": url}

        fetcher = cached_fetcher(fake, cache_dir=tmp_path)
        assert fetcher("http://example/a") == {"url": "http://example/a"}
        assert fetcher("http://example/b") == {"url": "http://example/b"}
        assert len(calls) == 2

    def test_stale_entry_is_treated_as_a_miss(self, tmp_path):
        calls = []

        def fake(url):
            calls.append(url)
            return {"n": len(calls)}

        now = [1_700_000_000.0]
        fetcher = cached_fetcher(fake, cache_dir=tmp_path, clock=lambda: now[0])

        assert fetcher("http://example/x") == {"n": 1}
        assert fetcher("http://example/x") == {"n": 1}  # still fresh
        assert len(calls) == 1

        now[0] += DEFAULT_TTL_SECONDS + 1  # past the 6h TTL
        assert fetcher("http://example/x") == {"n": 2}
        assert len(calls) == 2

    def test_stale_entry_via_direct_timestamp_manipulation(self, tmp_path):
        """Same idea as the clock-injection test, but exercised by writing
        directly to the cache file's stored timestamp, per the acceptance
        criteria's alternate approach."""
        import json
        import time

        calls = []

        def fake(url):
            calls.append(url)
            return {"n": len(calls)}

        cache_path = tmp_path / "raiderio.json"
        url = "http://example/x"
        stale_ts = time.time() - DEFAULT_TTL_SECONDS - 60
        cache_path.write_text(
            json.dumps({url: {"ts": stale_ts, "data": {"n": 0}}}),
            encoding="utf-8",
        )

        fetcher = cached_fetcher(fake, cache_dir=tmp_path)
        assert fetcher(url) == {"n": 1}
        assert len(calls) == 1

    def test_failed_fetch_is_not_cached(self, tmp_path):
        calls = []

        def failing(url):
            calls.append(url)
            return None

        fetcher = cached_fetcher(failing, cache_dir=tmp_path)
        assert fetcher("http://example/x") is None
        assert fetcher("http://example/x") is None
        assert len(calls) == 2  # never cached, so retried both times
        assert not (tmp_path / "raiderio.json").exists()

    def test_corrupt_cache_file_does_not_crash(self, tmp_path):
        cache_path = tmp_path / "raiderio.json"
        cache_path.write_text("{not valid json at all", encoding="utf-8")

        calls = []

        def fake(url):
            calls.append(url)
            return {"ok": True}

        fetcher = cached_fetcher(fake, cache_dir=tmp_path)
        assert fetcher("http://example/x") == {"ok": True}
        assert len(calls) == 1
        # the corrupt file gets overwritten with a well-formed cache now
        assert fetcher("http://example/x") == {"ok": True}
        assert len(calls) == 1

    def test_non_dict_cache_file_does_not_crash(self, tmp_path):
        cache_path = tmp_path / "raiderio.json"
        cache_path.write_text("[1, 2, 3]", encoding="utf-8")

        calls = []

        def fake(url):
            calls.append(url)
            return {"ok": True}

        fetcher = cached_fetcher(fake, cache_dir=tmp_path)
        assert fetcher("http://example/x") == {"ok": True}
        assert len(calls) == 1

    def test_separate_filenames_share_a_directory_without_colliding(self, tmp_path):
        """A future cached data source (e.g. dungeon-timer static data)
        should be able to reuse the same cache directory under its own
        filename."""
        calls_a, calls_b = [], []

        fetcher_a = cached_fetcher(
            lambda url: calls_a.append(url) or {"which": "a"},
            filename="a.json",
            cache_dir=tmp_path,
        )
        fetcher_b = cached_fetcher(
            lambda url: calls_b.append(url) or {"which": "b"},
            filename="b.json",
            cache_dir=tmp_path,
        )

        assert fetcher_a("http://example/x") == {"which": "a"}
        assert fetcher_b("http://example/x") == {"which": "b"}
        assert (tmp_path / "a.json").exists()
        assert (tmp_path / "b.json").exists()
        # each still caches independently
        assert fetcher_a("http://example/x") == {"which": "a"}
        assert fetcher_b("http://example/x") == {"which": "b"}
        assert len(calls_a) == 1
        assert len(calls_b) == 1
