"""HardenedProfile URL form."""


def test_profile_url_is_percent_encoded_and_round_trips(tmp_path):
    """-env:UserInstallation is a URL: an unencoded '#', '?' or '%' in the profile dir made
    LibreOffice (and any URL parser) resolve a different directory than the one hardened."""
    from urllib.parse import unquote, urlparse

    from clippyshot.libreoffice.profile import HardenedProfile

    root = tmp_path / "x#y a%20b?c"
    url = HardenedProfile(root).url()
    assert url == root.resolve().as_uri()
    parsed = urlparse(url)
    assert not parsed.fragment and not parsed.query
    assert unquote(parsed.path) == str(root.resolve())
