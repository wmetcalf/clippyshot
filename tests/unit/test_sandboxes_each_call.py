"""Which selections sandbox EVERY command, and therefore forbid a warm helper outside it.

An exact-name check silently stopped matching once the selection was decorated:
`CLIPPYSHOT_INNER_NONO=1` yields a `NonoWrappedSandbox` named `nsjail+nono`, so the guard
was off for exactly the deployment that had asked for MORE confinement (codex on #41).
"""
from __future__ import annotations

from clippyshot.sandbox.detect import sandboxes_each_call


class _Named:
    def __init__(self, name, inner=None):
        self.name = name
        if inner is not None:
            self.inner = inner


class TestPerCallBackendsAreRecognised:
    def test_nsjail(self):
        assert sandboxes_each_call(_Named("nsjail")) is True

    def test_bwrap(self):
        assert sandboxes_each_call(_Named("bwrap")) is True

    def test_container_is_not(self):
        """The guest/container tier IS the boundary; a warm helper there is fine."""
        assert sandboxes_each_call(_Named("container")) is False

    def test_nono_counts_too(self):
        """`NonoWrappedSandbox.run()` is `inner.run(wrap.apply(request))` -- Landlock per
        request. A warm server outside it skips that layer exactly as it would skip
        nsjail's (codex)."""
        assert sandboxes_each_call(_Named("nono")) is True

    def test_container_plus_nono_counts(self):
        """The container is the outer boundary, but nono still confines each CALL, so a
        warm helper started outside it is unconfined by Landlock while the cold path is
        not. A supported configuration on runc and in the Firecracker guest."""
        assert sandboxes_each_call(_Named("container+nono")) is True


class TestDecorationDoesNotHideIt:
    def test_the_real_nono_wrapper(self):
        """The actual class, not a stand-in: its `name` is built in __post_init__."""
        from clippyshot.sandbox.nono_wrap import NonoWrap, NonoWrappedSandbox

        wrapped = NonoWrappedSandbox(inner=_Named("nsjail"), wrap=NonoWrap())
        assert wrapped.name == "nsjail+nono"
        assert sandboxes_each_call(wrapped) is True, (
            "CLIPPYSHOT_INNER_NONO turned the guard off"
        )

    def test_a_composite_name(self):
        assert sandboxes_each_call(_Named("bwrap+nono")) is True

    def test_a_decorator_that_renames_entirely(self):
        """Name-matching alone is not enough; the chain is walked."""
        assert sandboxes_each_call(_Named("custom-wrapper", inner=_Named("nsjail"))) is True

    def test_a_container_chain_stays_false(self):
        assert sandboxes_each_call(_Named("wrapper", inner=_Named("container"))) is False


class TestItCannotHangWarmup:
    def test_a_self_referential_chain_terminates(self):
        """A decorator holding itself must not spin the warmup path forever.

        Run in a THREAD with a deadline: without the bound this call never returns, and a
        test that simply calls it turns a broken guard into a hung CI job rather than a
        failing one -- the mutation check for the bound hung for ten minutes before this
        was written that way.
        """
        import threading

        loop = _Named("wrapper")
        loop.inner = loop
        result: list = []

        def _call() -> None:
            result.append(sandboxes_each_call(loop))

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(5.0)
        assert not t.is_alive(), "the walk did not terminate on a cyclic decorator chain"
        assert result == [False]

    def test_something_without_a_name_is_not_per_call(self):
        assert sandboxes_each_call(object()) is False
        assert sandboxes_each_call(None) is False
