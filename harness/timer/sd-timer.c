/*
 * sd-timer: the harness's in-container clock.
 *
 *   sd-timer <timing-file> <uid> <gid> -- <command> [args...]
 *
 * Runs as root and PID 1 inside the submission's container. It drops the
 * submission to an unprivileged uid/gid, execs it, and times exec to exit
 * with CLOCK_MONOTONIC, so docker's 300-700 ms of container start and
 * teardown never lands in T. The timing file sits on a root-only mount the
 * submission cannot write, and an unprivileged process cannot signal or
 * ptrace a root one, so the number cannot be forged from inside.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static pid_t child = -1;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static long long now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

int main(int argc, char **argv) {
    if (argc < 6 || strcmp(argv[4], "--") != 0) {
        fprintf(stderr, "usage: sd-timer <timing-file> <uid> <gid> -- <command> [args...]\n");
        return 125;
    }
    const char *timing_path = argv[1];
    uid_t uid = (uid_t)strtoul(argv[2], NULL, 10);
    gid_t gid = (gid_t)strtoul(argv[3], NULL, 10);
    char **cmd = &argv[5];

    signal(SIGTERM, forward);
    signal(SIGINT, forward);

    long long start = now_ns();
    child = fork();
    if (child < 0) {
        perror("sd-timer: fork");
        return 125;
    }
    if (child == 0) {
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        if (setgroups(0, NULL) != 0 || setgid(gid) != 0 || setuid(uid) != 0) {
            perror("sd-timer: drop privileges");
            _exit(126);
        }
        execvp(cmd[0], cmd);
        fprintf(stderr, "sd-timer: exec %s: %s\n", cmd[0], strerror(errno));
        _exit(127);
    }

    /* As PID 1 we also reap anything the submission orphaned; only our own
     * child's exit stops the clock. */
    int status = 0;
    for (;;) {
        int st;
        pid_t done = wait(&st);
        if (done == child) {
            status = st;
            break;
        }
        if (done < 0 && errno != EINTR) {
            perror("sd-timer: wait");
            return 125;
        }
    }
    long long elapsed = now_ns() - start;

    int exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    int term_signal = WIFSIGNALED(status) ? WTERMSIG(status) : 0;

    FILE *f = fopen(timing_path, "w");
    if (!f) {
        perror("sd-timer: open timing file");
        return 125;
    }
    fprintf(f, "{\"wall_ns\": %lld, \"exit_code\": %d, \"signal\": %d}\n", elapsed, exit_code, term_signal);
    fflush(f);
    fsync(fileno(f));
    fclose(f);

    if (term_signal) return 128 + term_signal;
    return exit_code;
}
