# AppArmor profiles for ClippyShot

ClippyShot ships AppArmor profiles in this directory as defense-in-depth on
top of the bwrap/nsjail sandbox layers. There are two categories of profile,
covering different concerns.

## 1. User-namespace enablement profiles (load-bearing on Ubuntu 24.04+)

- `clippyshot-bwrap` — grants `userns,` to `/usr/bin/bwrap`
- `clippyshot-nsjail` — grants `userns,` to `/usr/local/bin/nsjail`

These two profiles are required on hosts where
`kernel.apparmor_restrict_unprivileged_userns=1` (the default on Ubuntu
24.04). Without them, bwrap and nsjail cannot create user namespaces and
ClippyShot cannot run.

**Automated (recommended):** `clippyshot setup-sandbox` probes whether `bwrap`/`nsjail`
can actually create a user namespace, and whether `aa-exec` can attach `clippyshot-soffice`
inside bwrap, and prints exactly which profiles need loading; `clippyshot setup-sandbox
--apply` loads them via `sudo` (interactive — the password prompt is your consent; it never
escalates silently). Idempotent — a no-op once loaded. If it reports a
`bwrap-userns-restrict` CONFLICT, see [below](#ubuntus-stock-bwrap-userns-restrict-profile).

**Loading manually** (one-time, survives reboots):

```sh
sudo cp deploy/apparmor/clippyshot-bwrap deploy/apparmor/clippyshot-nsjail /etc/apparmor.d/
sudo apparmor_parser -r -W /etc/apparmor.d/clippyshot-bwrap
sudo apparmor_parser -r -W /etc/apparmor.d/clippyshot-nsjail
```

**Verification:**

```sh
sudo aa-status | grep clippyshot
# clippyshot-bwrap
# clippyshot-nsjail
```

### Ubuntu's stock `bwrap-userns-restrict` profile

Newer Ubuntu releases (apparmor 4.1+, and the current GitHub-hosted runner images) ship
`/etc/apparmor.d/bwrap-userns-restrict`. It loads two profiles, `bwrap` (attached to
`/usr/bin/bwrap`) and `unpriv_bwrap`, and stacks every child bwrap starts into
`unpriv_bwrap`, which may not change profile. It conflicts with ClippyShot's bwrap backend
in both of its states:

| Loaded alongside... | What fails |
|---|---|
| nothing (stock profile only) | `aa-exec -p clippyshot-soffice` inside bwrap: `aa-exec: ERROR: insufficient permissions to change to the profile` |
| `clippyshot-bwrap` (both attach `/usr/bin/bwrap`) | bwrap itself: `setting up uid map: Permission denied` |

ClippyShot fails closed either way: it never runs soffice without `clippyshot-soffice`.
At startup (`select_sandbox()`, so also `clippyshot selftest` and the engine's first job)
the bwrap backend runs the attach probe CI runs,

```sh
bwrap --unshare-user --die-with-parent --ro-bind / / --proc /proc -- \
    aa-exec -p clippyshot-soffice -- /bin/true
```

(`--proc /proc` like `BwrapSandbox`; without a pid namespace that is a writable bind of the
host procfs, but either way aa-exec can write `/proc/self/attr/exec`, which the inherited
read-only `/proc` does not allow on some libapparmor versions). On failure it refuses to start with an
error naming `bwrap-userns-restrict` and the fix command. A passing probe is cached for
the life of the process; a failing one is re-run on every selection.

**Know what `clippyshot-bwrap` costs, on any host.** `clippyshot-bwrap` is
`flags=(unconfined) { userns, }` and attaches by path to every `/usr/bin/bwrap` on the host
(and in containers on it). Wherever it is loaded, ANY local user or bwrap consumer can get a
capability-bearing user namespace through `/usr/bin/bwrap` (e.g.
`bwrap --unshare-user --uid 0 --cap-add ALL ... mount ...` succeeds), which is exactly the
hole Ubuntu's unprivileged-userns restriction closes. That is true on a stock 24.04 host
with section 1's profiles loaded, not only after disabling the stock profile.
`bwrap-userns-restrict` is the distro's own mitigation for it: it lets bwrap create a user
namespace but stacks every bwrap child into `unpriv_bwrap`, which denies capabilities;
disabling it removes that. ClippyShot attaches `clippyshot-soffice` to its sandboxed children
by default (soffice, the cold QR/OCR scanners, pdftoppm); the pdfium rasterizer and the warm
OCR helper opt out and run under bwrap without a profile. Load the bwrap profiles only on a
host (or VM) dedicated to ClippyShot, with no untrusted local users; on a shared host, run
ClippyShot in its own VM instead, or use a deployment mode that does not run bwrap (gVisor or
Firecracker, below).

**Fix (persistent, dedicated hosts only):**

```sh
clippyshot setup-sandbox                                # reports the conflict, exits 1
clippyshot setup-sandbox --disable-stock-bwrap-profile  # fixes it (sudo, interactive)
```

The flag is the explicit opt-in to change distro policy; `--apply` alone will not touch
bwrap policy on a host in this state (loading `clippyshot-bwrap` next to the stock profile
only swaps one failure for the other). It still loads profiles the stock profile does not
affect, such as `clippyshot-nsjail` and `clippyshot-soffice`, so an nsjail host can keep the distro hardening. With the flag, setup runs, skipping any step already done:

```sh
sudo ln -sf /etc/apparmor.d/bwrap-userns-restrict /etc/apparmor.d/disable/bwrap-userns-restrict
sudo apparmor_parser -R /etc/apparmor.d/bwrap-userns-restrict
# then, for each of clippyshot-bwrap clippyshot-nsjail clippyshot-soffice clippyshot-runtime:
sudo cp deploy/apparmor/<profile> /etc/apparmor.d/
sudo apparmor_parser -r -W /etc/apparmor.d/<profile>
```

and then re-runs the attach probe, exiting non-zero if it still fails. Re-running it is a
no-op. The `disable/` symlink is what keeps the profile from returning on reboot (otherwise
`apparmor.service` reloads it next to `clippyshot-bwrap` at boot and bwrap fails with
`setting up uid map: Permission denied`) or on an `apparmor` package upgrade;
`apparmor_parser -R` alone only lasts until the next boot. CI
(`.github/workflows/ci.yml`) runs only the unload, because its runner is ephemeral.

`clippyshot selftest` (the image healthcheck) reports both `apparmor_attach` (`ok`, or
`not_applicable` for a non-bwrap backend, or `no_aa_exec` for a bwrap without aa-exec) and
`stock_bwrap_profile` (its state) in `selftest_passed`; on failure, the probe's cause is a
field only when bwrap was forced (`CLIPPYSHOT_SANDBOX=bwrap`; under auto-selection it is in
the error text), and logs a `stock_bwrap_profile_conflict` warning when the stock
profile is active on a host that has bwrap, even if nsjail was selected, so a host upgrade
that brings it back is visible before it breaks a bwrap deployment. (Not under the
`container` backend, where `/etc/apparmor.d` is the image's, not host policy.)

### Which deployments need this

| Deployment | ClippyShot sandbox | Stock-profile host prep? |
|---|---|---|
| Bare host (bwrap backend) | `bwrap` + `aa-exec` | **Yes**, dedicated hosts only (see the cost above): `clippyshot setup-sandbox --disable-stock-bwrap-profile` |
| Plain `docker run` of the ClippyShot image with bwrap selected | `bwrap` + `aa-exec` in the container, host kernel policy | **Yes**, on the host if it ships the stock profile (setup-sandbox in a container cannot change host policy; use the commands above). The container must also run under an AppArmor profile that allows the change to `clippyshot-soffice`: under Docker's default `docker-default`, aa-exec is refused whatever the host has (`change_profile_denied`); otherwise use `CLIPPYSHOT_SANDBOX=container` |
| gVisor (compose cold workers, `docker-compose.gvisor.yml` warm sidecar) | `container` (`CLIPPYSHOT_SANDBOX=container`) | No |
| Firecracker via blastbox (`docker-compose.firecracker.yml`) | `container`, inside the guest kernel | No |

- **gVisor.** AppArmor is not enforced for processes inside a gVisor sandbox: they run on
  gVisor's Sentry, not on host-kernel syscalls, and runsc ignores the container's OCI
  AppArmor profile (`docker inspect` may still show `docker-default`): only a profile that
  attaches to the runsc binary itself confines it on the host. So ClippyShot
  must use `CLIPPYSHOT_SANDBOX=container` there; the cold-worker image sets it (`ENV` in
  blastbox's `deploy/docker/Dockerfile.clippyshot-cold-worker`) and
  `docker-compose.gvisor.yml` sets it for warm slots via `BLASTBOX_GVISOR_EXTRA_ENV`. bwrap
  and aa-exec are never run, so the stock profile needs no host prep.
- **Firecracker.** The guest has its own kernel; host AppArmor policy does not reach guest
  processes. The FC rootfs (blastbox `deploy/firecracker/Dockerfile.clippyshot`, layered on
  this repo's image) bakes `deploy/firecracker/guest.clippyshot.env` as
  `/opt/blastbox/guest.env`, which `/init` sources and which exports
  `CLIPPYSHOT_SANDBOX=container` and `CLIPPYSHOT_WARN_ON_INSECURE=1`. The forced backend
  means no nsjail/bwrap fallback, so the guest never runs bwrap or aa-exec (the guest
  `/init` does not even mount securityfs). The guest image therefore needs nothing for this;
  the host needs only what blastbox's Firecracker prep requires.

Host preparation for the blastbox runtimes themselves (KVM, runsc, Docker) is in
blastbox's [docs/HOST-PREP.md](https://github.com/wmetcalf/blastbox/blob/main/docs/HOST-PREP.md).

### Containers on Ubuntu 24.04+ hosts

Important: Ubuntu 24.04's user-namespace restriction is enforced at the
**host kernel** level, so it applies to processes inside Docker containers
too. If you run the ClippyShot Docker image on an Ubuntu 24.04 host without
loading these two profiles, `bwrap` and `nsjail` inside the container will
fail to create user namespaces with:

```
clone(flags=...|CLONE_NEWUSER|...) failed: Operation not permitted
```

The fix is the same: load the profiles on the host. AppArmor profiles attach
by absolute exec path, so the kernel applies them to `/usr/bin/bwrap` and
`/usr/local/bin/nsjail` whether they run on the host or inside a container.
You do NOT need to modify the Dockerfile or the container runtime flags.

## 2. Strict per-process profiles (defense-in-depth)

- `clippyshot-soffice` — strict profile for the soffice process running
  inside the sandbox. Denies all network (`deny network,`), denies
  `ptrace`/`mount`/`pivot_root`/raw sockets/`sys_admin`/`sys_module`, allows
  read-only access to LibreOffice install paths and fonts, and read-write
  only on the sandbox bind mounts. **Attached to soffice via
  `aa_change_onexec()` by both sandbox backends** (see below).
- `clippyshot-runtime` — looser profile for the ClippyShot Python runtime
  itself (CLI and HTTP server). Allows binding a TCP listen port and
  exec-ing the sandbox binaries. Still denies raw network, ptrace, and
  mount.

### How `clippyshot-soffice` actually attaches to soffice

This section used to be a TODO — the profile was dead code. It now attaches
via two paths, one per sandbox backend:

- **nsjail:** `src/clippyshot/sandbox/nsjail.py` passes
  `--proc_apparmor=clippyshot-soffice` in the argv it builds. nsjail calls
  `aa_change_onexec("clippyshot-soffice")` before `execve()`-ing the child.
- **bwrap:** `src/clippyshot/sandbox/bwrap.py` prefixes the inner argv with
  `/usr/bin/aa-exec -p clippyshot-soffice -- …`. The `aa-exec` helper
  (Ubuntu package `apparmor-utils`, installed in the ClippyShot runtime
  image) performs the same `aa_change_onexec()` call and then `exec`s the
  target.

Neither path runs `aa_change_onexec()` directly from Python. Both rely on
the profile being loaded on the host kernel at the time the sandbox
execs — if it isn't, the exec fails loudly with a clear error instead of
silently running soffice unconfined.

### Required host setup

- Load `clippyshot-soffice` via `apparmor_parser -r -W
  /etc/apparmor.d/clippyshot-soffice` (see the load command below).
- Install `apparmor-utils` (for `aa-exec`) if using the bwrap backend on
  a host outside the ClippyShot runtime image. The runtime image already
  installs it.

Both profiles are optional defense-in-depth — ClippyShot runs safely without
them, but they are recommended for production deployments where the operator
controls the host kernel.

**Loading:**

```sh
sudo cp deploy/apparmor/clippyshot-soffice deploy/apparmor/clippyshot-runtime /etc/apparmor.d/
sudo apparmor_parser -r -W /etc/apparmor.d/clippyshot-soffice
sudo apparmor_parser -r -W /etc/apparmor.d/clippyshot-runtime
```

## Running the container with the strict profile

```sh
docker run --rm \
    --read-only \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --security-opt apparmor=clippyshot-runtime \
    --tmpfs /tmp:rw,exec,nosuid,size=512m \
    --tmpfs /var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001 \
    clippyshot:dev serve
```

The `clippyshot-soffice` profile attaches via `Px ->` from
`clippyshot-runtime` when the runtime exec's bwrap/nsjail, which in turn
exec's soffice. Profile transitions are documented in `clippyshot-runtime`.

## Host portability

| Host | User-namespace profiles needed? | Strict profiles supported? |
|---|---|---|
| Ubuntu 24.04+ self-managed | Yes | Yes (recommended for prod) |
| Ubuntu &le;22.04, Debian 12, Amazon Linux | No | Yes |
| ECS / EKS on Amazon Linux (EC2 launch type) | No | Yes (load via user-data) |
| EKS (managed) | Depends on node AMI | Yes (DaemonSet to load profiles) |
| ECS / EKS Fargate | No | No (managed AppArmor) |
| Cloud Run | No | No (managed) |
| AWS Lambda | N/A (image too large) | N/A |

ClippyShot itself does not depend on the strict profiles being loaded — it
depends on bwrap or nsjail being able to create user namespaces. On hosts
with the Ubuntu 24.04 hardening, the userns enablement profiles in §1 are
how you opt the sandbox binaries in. The strict profiles in §2 are added
defense in depth on top of that.

## bwrap fork-bomb defense and PIDs limit

The nsjail backend enforces `--rlimit_nproc 256` inside the new user namespace,
which works because nsjail creates a fresh user namespace whose uid has zero
existing processes. The bwrap backend cannot use `RLIMIT_NPROC` in its preexec
function because that limit is scoped to the **real uid on the host** — not
the sandboxed namespace. Setting NPROC on a shared uid causes all bwrap
invocations to fail with "Resource temporarily unavailable" once the host's
existing process count approaches the limit.

Instead, the bwrap backend probes `bwrap --help` for `--cgroup-pids` at construction
time and, if present, adds `--cgroup-pids 256` to every invocation. No released
bubblewrap has that option (checked through 0.11), so in practice the probe fails, the
backend reports `pid_limit_missing`, and `select_sandbox` treats bwrap as insecure unless
`CLIPPYSHOT_WARN_ON_INSECURE=1`.

When `--cgroup-pids` is not available, ClippyShot logs
a `WARN` and **falls back to relying on the container runtime's PIDs limit**.
Configure this at the deployment layer:

- **Docker:** `docker run --pids-limit 256 …`
- **Kubernetes:** set `spec.containers[].resources.limits.pids` (requires
  SupportPodPidsLimit feature gate, enabled by default since k8s 1.24)
- **ECS:** set `pidLimit` in the container definition (Linux-only)

The `RLIMIT_NPROC` comment in `src/clippyshot/sandbox/bwrap.py` documents why
the per-uid rlimit is intentionally absent.

## Defense in depth (spec §11)

Each layer is independently sufficient to stop most attacks; they compose:

1. **Magika-validated input type** — malformed-on-purpose files are rejected
   before soffice sees them.
2. **Hardened LibreOffice profile** + flags — macros, Basic, Java, updates,
   and remote resources are off at the application layer.
3. **nsjail / bwrap sandbox** — namespace isolation, seccomp, no network,
   rlimits, no capabilities.
4. **AppArmor profile** (when available) — kernel MAC policy enforcing
   file, network, and exec restrictions independent of the sandbox.
5. **Unprivileged container user + read-only rootfs** — the blast radius is
   confined even if every layer above fails.
