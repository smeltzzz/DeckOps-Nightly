"""net: size/rate formatting, digest checks and resume validators (no network)."""

import hashlib
import json

import pytest

import identity
import net


# -- formatting --------------------------------------------------------------

@pytest.mark.parametrize("n, want", [
    (0, "0 B"),
    (1023, "1023 B"),
    (1024, "1 KB"),
    (1 << 20, "1.0 MB"),
    (100 << 20, "100 MB"),
    (3 << 30, "3.0 GB"),
])
def test_fmt_size(n, want):
    assert net._fmt_size(n) == want


@pytest.mark.parametrize("bps, want", [
    (0, "0 B/s"),
    (2048, "2 KB/s"),
    (5 << 20, "5.0 MB/s"),
])
def test_fmt_rate(bps, want):
    assert net._fmt_rate(bps) == want


def test_detail_with_and_without_total_and_rate():
    d = net._detail("Plutonium", 1 << 20, 4 << 20, 1 << 20)
    assert d.startswith("Plutonium  1.0 MB / 4.0 MB") and d.endswith("1.0 MB/s")
    assert net._detail("", 2048, 0, 0) == "Downloading  2 KB"


# -- digest check ------------------------------------------------------------

def test_check_digest_accepts_matching_file(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    net._check_digest(str(f), "sha256:" + hashlib.sha256(b"payload").hexdigest().upper())
    assert f.exists()


def test_check_digest_rejects_and_deletes_bad_file(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    with pytest.raises(OSError, match="sha256 mismatch"):
        net._check_digest(str(f), "sha256:" + "0" * 64)
    assert not f.exists()  # a corrupt download must not be left to be reused


# -- resume validators -------------------------------------------------------

def test_validator_prefers_strong_etag(tmp_path):
    meta = tmp_path / "x.part.meta"
    net._write_validator(str(meta), {"ETag": '"abc"', "Last-Modified": "Mon"})
    assert net._read_validator(str(meta)) == '"abc"'


def test_validator_falls_back_to_last_modified_for_weak_etag(tmp_path):
    meta = tmp_path / "x.part.meta"
    net._write_validator(str(meta), {"ETag": 'W/"abc"', "Last-Modified": "Mon, 01 Jan 2024"})
    assert net._read_validator(str(meta)) == "Mon, 01 Jan 2024"


def test_validator_without_headers_clears_any_old_one(tmp_path):
    meta = tmp_path / "x.part.meta"
    meta.write_text(json.dumps({"if_range": "old"}))
    net._write_validator(str(meta), {})
    assert not meta.exists()
    assert net._read_validator(str(meta)) is None


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", '{"if_range": ""}'])
def test_read_validator_tolerates_garbage(tmp_path, content):
    meta = tmp_path / "m"
    meta.write_text(content)
    assert net._read_validator(str(meta)) is None


# -- identity URLs -----------------------------------------------------------

def test_identity_urls_follow_the_repo_constants():
    assert identity.asset_url("assets/images/icon.png") == (
        f"https://raw.githubusercontent.com/{identity.GITHUB_USER}/{identity.GITHUB_REPO}"
        "/refs/heads/main/assets/images/icon.png")
    assert identity.api_url("commits/main") == (
        f"https://api.github.com/repos/{identity.GITHUB_USER}/{identity.GITHUB_REPO}/commits/main")
