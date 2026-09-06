# Malicious test fixtures

These files are deliberately crafted to verify that ClippyShot's hardening
prevents network access, OLE link updates, macro execution, and resource
exhaustion. None of them contain real exploits — they exercise *features* of
office formats that ClippyShot must refuse to honor.

Each file's expected behavior is asserted in
`tests/integration/test_security_assertions.py`. If you add a new fixture
here, document what it tests and add the assertion.

## Current fixtures

| File | Tests | Expected behavior |
|---|---|---|
| `external_image.docx` | Network egress denial | A docx that references a remote image at `http://127.0.0.1:65500/track.png`. ClippyShot must NOT attempt to fetch the image — verified by binding a TCP listener to that port and asserting zero connections. |
| `ole_link.rtf` | OLE link update denial | An RTF with an OLE `\object\objupdate` link. ClippyShot must not follow it; the rendered output must not contain any data the link would have produced. |
| `sleeper.csv` | Page truncation + timeout | A 20,000-row CSV. With `max_pages=1`, the conversion must produce exactly 1 page and `truncated=True`. With a 1-second `timeout_s` against the same input, the conversion must be killed. |
| `macro_autoopen.odt` | Macro execution denial | An ODT with an embedded Basic library containing a `Document_Open` handler that would write `/tmp/clippyshot-macro-pwned` if executed. The hardened profile (`MacroSecurityLevel=3`, `DisableMacrosExecution=true`) must prevent execution. Note what the sentinel check can and cannot show: LibreOffice does not fire `Document_Open` during a headless conversion at all (measured on 24.2.7.2 with a default profile and no sandbox), and the sandbox's `/tmp` is a tmpfs, so the absence of the sentinel is a HOST-isolation result. The profile settings are asserted directly in `tests/unit/test_libreoffice_profile.py`. |
