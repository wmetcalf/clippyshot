/*
 * Sandbox escape probe.
 *
 * Tries a small set of operations that a confined process should not be
 * able to perform. Prints BLOCKED/LEAKED for each. Exits 0 if every attempt
 * was blocked (good), nonzero if any unexpectedly succeeded (bad).
 *
 * Compiled and run inside the sandbox via Sandbox.run() in the test.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <netinet/in.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/ptrace.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

static int blocked = 0;
static int leaks = 0;

static void check(const char *what, int rc, int err) {
    if (rc < 0) {
        fprintf(stdout, "BLOCKED %s: %s\n", what, strerror(err));
        blocked++;
    } else {
        fprintf(stdout, "LEAKED %s\n", what);
        leaks++;
    }
}

int main(void) {
    /* mount a tmpfs on /mnt — should fail */
    int rc = mount("none", "/mnt", "tmpfs", 0, NULL);
    check("mount", rc, errno);

    /* unshare CLONE_NEWUSER — should fail */
    rc = unshare(CLONE_NEWUSER);
    check("unshare(CLONE_NEWUSER)", rc, errno);

    /* ptrace ourselves */
    rc = ptrace(PTRACE_TRACEME, 0, NULL, NULL);
    check("ptrace", rc, errno);

    /* raw socket */
    rc = socket(AF_INET, SOCK_RAW, IPPROTO_TCP);
    check("raw_socket", rc, errno);

    /* connect to localhost:1 — should fail (no net or no listener) */
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s >= 0) {
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons(1);
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        rc = connect(s, (struct sockaddr *)&addr, sizeof(addr));
        check("connect_lo:1", rc, errno);
        close(s);
    } else {
        fprintf(stdout, "BLOCKED stream_socket: %s\n", strerror(errno));
        blocked++;
    }

    /* bpf(BPF_PROG_LOAD) — should fail with EPERM under the seccomp policy. */
    union bpf_attr battr;
    memset(&battr, 0, sizeof(battr));
    long brc = syscall(SYS_bpf, BPF_PROG_LOAD, &battr, sizeof(battr));
    check("bpf(BPF_PROG_LOAD)", (int)brc, errno);

    /* keyctl — should fail with EPERM under the seccomp policy. */
    long krc = syscall(SYS_keyctl, 0, 0, 0, 0, 0);
    check("keyctl", (int)krc, errno);

    fprintf(stdout, "SUMMARY blocked=%d leaks=%d\n", blocked, leaks);
    return leaks == 0 ? 0 : 1;
}
