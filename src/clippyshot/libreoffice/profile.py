"""Generate a hardened LibreOffice user profile."""
from __future__ import annotations

from pathlib import Path

# Each tuple: (oor:path, prop name, type, value)
_REGMODS: list[tuple[str, str, str, str]] = [
    # LO's MacroSecurityLevel enum is 0-3: Low/Medium/High/Very High. Values
    # >=4 are out of range and LO silently ignores them (falling back to a
    # default), so the old "4" here was a silent no-op. Use 3 (Very High),
    # the strictest defined level. DisableMacrosExecution below is the
    # separate nuclear-option flag that actually prevents macro execution
    # regardless of this setting.
    ("/org.openoffice.Office.Common/Security/Scripting", "MacroSecurityLevel", "int", "3"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableMacrosExecution", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "OfficeBasic", "int", "0"),
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "RemovePersonalInfoOnSaving", "boolean", "true"),
    ("/org.openoffice.Office.Common/Misc", "UseSystemFileDialog", "boolean", "false"),
    ("/org.openoffice.Office.Common/Misc", "FirstRun", "boolean", "false"),
    ("/org.openoffice.Office.Common/Internet/Proxy", "Type", "int", "0"),
    ("/org.openoffice.Office.Common/Load", "UseRegistrySettings", "boolean", "false"),
    ("/org.openoffice.Office.Common/Load", "URL", "string", ""),
    ("/org.openoffice.Office.ExtensionManager/ExtensionUpdateData", "AutoCheckEnabled", "boolean", "false"),
    ("/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job['UpdateCheck']/Arguments", "AutoCheckEnabled", "boolean", "false"),
    ("/org.openoffice.Office.Java/VirtualMachine", "NetAccess", "int", "3"),
]


_XCU_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema">
"""

_XCU_FOOTER = "</oor:items>\n"


_JAVA_SETTINGS = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<java xmlns="http://openoffice.org/2004/java/framework/1.0"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <enabled>false</enabled>
  <userClassPath xsi:nil="true"/>
  <vmParameters xsi:nil="true"/>
  <jreLocations xsi:nil="true"/>
  <javaInfo xsi:nil="true"/>
</java>
"""


class HardenedProfile:
    """A throwaway LibreOffice user installation locked down per spec §5."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write(self) -> Path:
        user = self.root / "user"
        config = user / "config"
        config.mkdir(parents=True, exist_ok=True)

        (user / "registrymodifications.xcu").write_text(self._render_xcu())
        (config / "javasettings_Linux_X86_64.xml").write_text(_JAVA_SETTINGS)
        return self.root

    def _render_xcu(self) -> str:
        parts: list[str] = [_XCU_HEADER]
        for path, name, type_, value in _REGMODS:
            parts.append(
                f'  <item oor:path="{path}">\n'
                f'    <prop oor:name="{name}" oor:type="xs:{type_}" oor:op="fuse">\n'
                f"      <value>{value}</value>\n"
                f"    </prop>\n"
                f"  </item>\n"
            )
        parts.append(_XCU_FOOTER)
        return "".join(parts)

    def url(self) -> str:
        """Return the file:// URL form expected by `-env:UserInstallation`."""
        return f"file://{self.root.resolve()}"
