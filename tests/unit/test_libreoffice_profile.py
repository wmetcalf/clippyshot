import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from clippyshot.libreoffice.profile import HardenedProfile


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    p = HardenedProfile(tmp_path / "lo-profile").write()
    return p


def _read_keys(xcu: Path) -> dict[str, str]:
    """Parse registrymodifications.xcu into a flat path/name -> value map.

    The <item> and <prop> elements are not namespace-qualified themselves, but
    their attributes use the oor: prefix (namespace URI
    http://openoffice.org/2001/registry).
    """
    tree = ET.parse(xcu)
    root = tree.getroot()
    out: dict[str, str] = {}
    ns = "http://openoffice.org/2001/registry"
    for item in root.iter("item"):
        path = item.get(f"{{{ns}}}path", "")
        for prop in item.iter("prop"):
            name = prop.get(f"{{{ns}}}name", "")
            for val in prop.iter("value"):
                out[f"{path}/{name}"] = (val.text or "").strip()
    return out


def test_profile_directory_layout(profile: Path):
    assert profile.is_dir()
    assert (profile / "user").is_dir()
    assert (profile / "user" / "registrymodifications.xcu").is_file()
    assert (profile / "user" / "config" / "javasettings_Linux_X86_64.xml").is_file()


def test_macro_security_locked_to_very_high(profile: Path):
    keys = _read_keys(profile / "user" / "registrymodifications.xcu")
    # LO's MacroSecurityLevel is 0-3 (Low/Medium/High/Very High). 3 = Very
    # High, the strictest defined level. Values >=4 are out of range and
    # LO silently ignores them, which was the bug this test previously
    # asserted.
    assert keys.get(
        "/org.openoffice.Office.Common/Security/Scripting/MacroSecurityLevel"
    ) == "3"
    assert keys.get(
        "/org.openoffice.Office.Common/Security/Scripting/DisableMacrosExecution"
    ) == "true"


def test_office_basic_disabled(profile: Path):
    keys = _read_keys(profile / "user" / "registrymodifications.xcu")
    assert keys.get(
        "/org.openoffice.Office.Common/Security/Scripting/OfficeBasic"
    ) == "0"


def test_java_disabled(profile: Path):
    js = (profile / "user" / "config" / "javasettings_Linux_X86_64.xml").read_text()
    assert "enabled" in js.lower()
    assert 'xsi:nil="true"' in js


def test_update_check_disabled(profile: Path):
    keys = _read_keys(profile / "user" / "registrymodifications.xcu")
    assert any("UpdateCheck" in k and v == "false" for k, v in keys.items())


def test_internet_proxy_set_to_none(profile: Path):
    keys = _read_keys(profile / "user" / "registrymodifications.xcu")
    assert keys.get("/org.openoffice.Office.Common/Internet/Proxy/Type") == "0"


def test_load_url_is_empty_string(profile: Path):
    keys = _read_keys(profile / "user" / "registrymodifications.xcu")
    assert keys.get("/org.openoffice.Office.Common/Load/URL") == ""


def test_extension_update_check_disabled(profile: Path):
    keys = _read_keys(profile / "user" / "registrymodifications.xcu")
    assert keys.get(
        "/org.openoffice.Office.ExtensionManager/ExtensionUpdateData/AutoCheckEnabled"
    ) == "false"


def test_url_returns_file_url(tmp_path: Path):
    hp = HardenedProfile(tmp_path / "lo-profile")
    hp.write()
    url = hp.url()
    assert url.startswith("file://")
    assert str((tmp_path / "lo-profile").resolve()) in url
