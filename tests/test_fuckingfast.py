"""Unit Tests for FuckingFast Link Resolution."""

import pytest
from resolvers.fuckingfast import FuckingFastResolver


def test_fuckingfast_url_parsing():
    url = "https://fuckingfast.co/xrhzu2j9ft3u#Phantom_Blade_Zero_--_fitgirl-repacks.site_--_.part01.rar"
    # Verify expected filename from fragment
    from urllib.parse import urlparse, unquote
    p = urlparse(url)
    assert unquote(p.fragment) == "Phantom_Blade_Zero_--_fitgirl-repacks.site_--_.part01.rar"
    path_segments = [seg for seg in p.path.split("/") if seg]
    assert path_segments[0] == "xrhzu2j9ft3u"


def test_fuckingfast_session_management():
    """Verifies that FuckingFastResolver maintains and caches persistent sessions per profile."""
    s1 = FuckingFastResolver._get_session(None, "chrome")
    s2 = FuckingFastResolver._get_session(None, "chrome")
    assert s1 is s2, "Should reuse session for identical interface & profile"

    FuckingFastResolver._evict_session(None, "chrome")
    assert (None, "chrome") not in FuckingFastResolver._sessions

    

