import pytest

from clippyshot.limits import Limits


def test_limits_defaults_match_spec():
    limits = Limits()
    assert limits.timeout_s == 60
    assert limits.memory_bytes == 8 * 1024 * 1024 * 1024
    assert limits.tmpfs_bytes == 1024 * 1024 * 1024
    assert limits.max_input_bytes == 100 * 1024 * 1024
    assert limits.max_pages == 50
    assert limits.dpi == 150


def test_limits_override_individual_field():
    limits = Limits(timeout_s=120)
    assert limits.timeout_s == 120
    assert limits.dpi == 150  # other defaults preserved


def test_limits_from_env(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_TIMEOUT", "30")
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGES", "10")
    limits = Limits.from_env()
    assert limits.timeout_s == 30
    assert limits.max_pages == 10
    assert limits.dpi == 150


def test_skip_blanks_bool_spellings(monkeypatch):
    # default-ON: canonical AND noncanonical falsy spellings (incl. whitespace/"off") disable it.
    for val in ("0", "false", "no", "Off", " NO ", "false "):
        monkeypatch.setenv("CLIPPYSHOT_SKIP_BLANKS", val)
        assert Limits.from_env().skip_blanks is False, val
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("CLIPPYSHOT_SKIP_BLANKS", val)
        assert Limits.from_env().skip_blanks is True, val
    monkeypatch.delenv("CLIPPYSHOT_SKIP_BLANKS")
    assert Limits.from_env().skip_blanks is True   # default on


def test_disclose_security_internals_fail_closed(monkeypatch):
    # a SECURITY flag (True = more disclosure) must FAIL CLOSED: only an explicit truthy token enables it,
    # so a typo / whitespace / "off" can't silently flip on security-internals disclosure.
    for val in ("1", "true", "YES", " on "):
        monkeypatch.setenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS", val)
        assert Limits.from_env().disclose_security_internals is True, val
    for val in ("0", "false", "off", "garbage", "No"):
        monkeypatch.setenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS", val)
        assert Limits.from_env().disclose_security_internals is False, val
    monkeypatch.delenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS")
    assert Limits.from_env().disclose_security_internals is False   # default off


def test_dpi_too_high_rejected():
    with pytest.raises(ValueError, match="dpi"):
        Limits(dpi=10000)


def test_dpi_too_low_rejected():
    with pytest.raises(ValueError, match="dpi"):
        Limits(dpi=10)


def test_max_pages_zero_rejected():
    with pytest.raises(ValueError, match="max_pages"):
        Limits(max_pages=0)


def test_timeout_too_long_rejected():
    with pytest.raises(ValueError, match="timeout_s"):
        Limits(timeout_s=10000)


def test_default_limits_pass_validation():
    Limits()  # should not raise


def test_the_worker_memory_budget_follows_the_blastbox_limits(monkeypatch):
    """Only CLIPPYSHOT_WORKER_MEMORY was consulted.

    A deployment that sets the blastbox-side limit and not this one budgeted
    every page against the 4 GB default -- twice what an FC guest at
    BLASTBOX_FC_MEM_MIB=2048 actually has.
    """
    from clippyshot.limits import parse_memory_gb, resolve_worker_memory

    for name in ("CLIPPYSHOT_WORKER_MEMORY", "BLASTBOX_WORKER_MEMORY", "BLASTBOX_FC_MEM_MIB"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("BLASTBOX_FC_MEM_MIB", "2048")
    assert parse_memory_gb(resolve_worker_memory()) == 2.0

    monkeypatch.setenv("BLASTBOX_WORKER_MEMORY", "3g")
    assert parse_memory_gb(resolve_worker_memory()) == 3.0, "the explicit spec beats the guest MiB"

    monkeypatch.setenv("CLIPPYSHOT_WORKER_MEMORY", "1g")
    assert parse_memory_gb(resolve_worker_memory()) == 1.0, "clippyshot's own name wins"

    assert resolve_worker_memory("512m") == "512m", "an explicit argument beats every env"


def test_detection_may_only_lower_the_budget(monkeypatch, tmp_path):
    """MemTotal in an unlimited container is the HOST's RAM.

    Budgeting a page against a 64 GB host would OOM the worker -- worse than
    the 4 GB assumption it replaced -- so a detected figure is used only when
    it is smaller.
    """
    import clippyshot.limits as limits

    for name in ("CLIPPYSHOT_WORKER_MEMORY", "BLASTBOX_WORKER_MEMORY", "BLASTBOX_FC_MEM_MIB"):
        monkeypatch.delenv(name, raising=False)

    big = tmp_path / "meminfo-big"
    big.write_text("MemTotal:       65225456 kB\nMemFree:  100 kB\n")
    monkeypatch.setattr(limits, "_CGROUP_MAX", str(tmp_path / "absent"))
    monkeypatch.setattr(limits, "_MEMINFO", str(big))
    assert limits.resolve_worker_memory() == "4g", "a 62 GB host must not raise the budget"

    small = tmp_path / "meminfo-small"
    small.write_text("MemTotal:        2097152 kB\n")
    monkeypatch.setattr(limits, "_MEMINFO", str(small))
    assert limits.parse_memory_gb(limits.resolve_worker_memory()) == 2.0, (
        "a 2 GB guest must lower it"
    )

    cg = tmp_path / "memory.max"
    cg.write_text("1073741824\n")  # 1 GiB, a real container limit
    monkeypatch.setattr(limits, "_CGROUP_MAX", str(cg))
    assert limits.parse_memory_gb(limits.resolve_worker_memory()) == 1.0, (
        "the cgroup limit is the container's real cap and beats MemTotal"
    )
