"""net.download_github_file and the gitblob digest, against a fake urlopen."""

import hashlib
import json
import urllib.error

import pytest

import net
from fakes import FakeUrlopen


def blob_sha(data: bytes) -> str:
    """What `git hash-object` prints for these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture
def gh(monkeypatch):
    fake = FakeUrlopen()
    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    monkeypatch.setattr(net.time, "sleep", lambda s: None)   # no retry backoff in tests
    return fake


API = "https://api.github.com/repos/owner/repo"
DATA = b"MZ pretend this is a DLL" * 100


def serve_file(gh, data=DATA, path="d3d11.dll", ref="main", blob_data=None):
    sha = blob_sha(data)
    gh.routes[f"{API}/contents/{path}?ref={ref}"] = json.dumps(
        {"type": "file", "sha": sha, "size": len(data), "name": path}).encode()
    gh.routes[f"{API}/git/blobs/{sha}"] = data if blob_data is None else blob_data
    return sha


# -- gitblob digest ----------------------------------------------------------

def test_gitblob_digest_accepts_matching_file(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"hello\n")
    net._check_digest(str(f), "gitblob:ce013625030ba8dba906f756967f9e9ca394464a")
    assert f.exists()


def test_gitblob_digest_rejects_and_deletes(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"hello\n")
    with pytest.raises(OSError, match="gitblob mismatch"):
        net._check_digest(str(f), "gitblob:" + "0" * 40)
    assert not f.exists()


# -- download_github_file ----------------------------------------------------

def test_downloads_the_exact_blob_the_api_named(gh, tmp_path):
    sha = serve_file(gh)
    dest = tmp_path / "d3d11.dll"
    net.download_github_file("owner/repo", "d3d11.dll", str(dest))
    assert dest.read_bytes() == DATA
    assert not (tmp_path / "d3d11.dll.part").exists()
    # Looked the file up, then fetched that blob (not the branch URL), asking
    # for the raw bytes.
    assert gh.requests == [
        (f"{API}/contents/d3d11.dll?ref=main", "application/vnd.github+json"),
        (f"{API}/git/blobs/{sha}", "application/vnd.github.raw"),
    ]


def test_ref_and_odd_paths_are_url_encoded(gh, tmp_path):
    # Slashes stay (valid in a path and in a query); spaces are encoded.
    serve_file(gh, path="dir%20x/a%20b.dll", ref="feature/x%20y")
    dest = tmp_path / "out"
    net.download_github_file("owner/repo", "dir x/a b.dll", str(dest), ref="feature/x y")
    assert dest.read_bytes() == DATA


def test_wrong_bytes_are_rejected_and_not_left_behind(gh, tmp_path):
    serve_file(gh, blob_data=DATA[:-1] + b"!")      # same length, one byte changed
    dest = tmp_path / "d3d11.dll"
    with pytest.raises(OSError, match="gitblob mismatch"):
        net.download_github_file("owner/repo", "d3d11.dll", str(dest), tries=2)
    assert not dest.exists()
    assert not (tmp_path / "d3d11.dll.part").exists()


def test_download_kwargs_are_passed_through(gh, tmp_path):
    serve_file(gh)
    seen = []
    net.download_github_file("owner/repo", "d3d11.dll", str(tmp_path / "x"),
                             on_progress=lambda p, m: seen.append((p, m)), label="CleanOps DLL")
    assert seen and seen[-1][0] == 100 and "CleanOps DLL" in seen[-1][1]


def test_lookup_failure_uses_fallback_unverified(gh, tmp_path):
    gh.routes[f"{API}/contents/d3d11.dll?ref=main"] = urllib.error.URLError("offline")
    gh.routes["https://example.com/fallback.dll"] = b"from the fallback"
    dest = tmp_path / "d3d11.dll"
    net.download_github_file("owner/repo", "d3d11.dll", str(dest),
                             fallback_url="https://example.com/fallback.dll")
    assert dest.read_bytes() == b"from the fallback"


def test_lookup_failure_without_fallback_raises(gh, tmp_path):
    gh.routes[f"{API}/contents/d3d11.dll?ref=main"] = urllib.error.URLError("offline")
    with pytest.raises(urllib.error.URLError):
        net.download_github_file("owner/repo", "d3d11.dll", str(tmp_path / "x"))
    assert not (tmp_path / "x").exists()


def test_a_directory_listing_is_not_a_file(gh, tmp_path):
    # The contents API answers with a list when path is a folder.
    gh.routes[f"{API}/contents/d3d11.dll?ref=main"] = b'[{"name": "a"}]'
    gh.routes["https://example.com/fb"] = b"fallback"
    net.download_github_file("owner/repo", "d3d11.dll", str(tmp_path / "x"),
                             fallback_url="https://example.com/fb")
    assert (tmp_path / "x").read_bytes() == b"fallback"
    with pytest.raises(RuntimeError, match="not a file"):
        net.download_github_file("owner/repo", "d3d11.dll", str(tmp_path / "y"))


def test_refused_blob_download_falls_back_but_still_verifies(gh, tmp_path):
    sha = serve_file(gh)
    gh.routes[f"{API}/git/blobs/{sha}"] = urllib.error.HTTPError(
        f"{API}/git/blobs/{sha}", 403, "rate limit exceeded", {}, None)
    gh.routes["https://example.com/good"] = DATA
    gh.routes["https://example.com/stale"] = b"an older build"

    ok = tmp_path / "ok.dll"
    net.download_github_file("owner/repo", "d3d11.dll", str(ok),
                             fallback_url="https://example.com/good", tries=1)
    assert ok.read_bytes() == DATA

    stale = tmp_path / "stale.dll"
    with pytest.raises(OSError, match="gitblob mismatch"):
        net.download_github_file("owner/repo", "d3d11.dll", str(stale),
                                 fallback_url="https://example.com/stale", tries=1)
    assert not stale.exists()


def test_refused_blob_download_without_fallback_raises(gh, tmp_path):
    sha = serve_file(gh)
    gh.routes[f"{API}/git/blobs/{sha}"] = urllib.error.HTTPError(
        f"{API}/git/blobs/{sha}", 403, "rate limit exceeded", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        net.download_github_file("owner/repo", "d3d11.dll", str(tmp_path / "x"), tries=1)


def test_cleanops_points_at_the_repo_and_file_it_pins():
    import cleanops
    assert cleanops.DLL_URL == f"https://raw.githubusercontent.com/{cleanops.REPO}/main/{cleanops.DLL_PATH}"
