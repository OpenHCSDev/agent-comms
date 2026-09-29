/* Kill the ACTUAL native writer at OS publication boundaries. Test-only preload;
 * no production hooks, fake session/proof, suppressed syscalls or provider calls.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

static int journal_seen;

static void crash_at(const char *point) {
    const char *wanted = getenv("PROOF_CRASH_POINT");
    const char *receipt = getenv("PROOF_CRASH_RECEIPT");
    if (!wanted || !receipt || strcmp(wanted, point)) return;
    int fd = open(receipt, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd >= 0) {
        write(fd, point, strlen(point));
        syscall(SYS_fsync, fd);
        close(fd);
    }
    kill(getpid(), SIGKILL);
    _exit(99);
}

static int sync_boundary(int fd, const char *symbol) {
    int (*real)(int) = dlsym(RTLD_NEXT, symbol);
    const char *proof = getenv("PROOF_CRASH_FILE");
    char name[64], path[PATH_MAX], journal[PATH_MAX], parent[PATH_MAX];
    if (!proof) return real(fd);
    snprintf(name, sizeof(name), "/proc/self/fd/%d", fd);
    ssize_t count = readlink(name, path, sizeof(path)-1);
    if (count < 0) return real(fd);
    path[count] = 0;
    snprintf(journal, sizeof(journal), "%s-journal", proof);
    snprintf(parent, sizeof(parent), "%s", proof);
    *strrchr(parent, '/') = 0;
    if (!strcmp(path, journal)) {
        journal_seen = 1;
        crash_at("before-journal-sync");
    }
    if (journal_seen && !strcmp(path, proof)) crash_at("before-database-sync");
    if (!strncmp(path, proof, strlen(proof)) &&
        strstr(path + strlen(proof), ".initializing-") &&
        !strstr(path, "-journal")) crash_at("before-schema-sync");
    if (journal_seen && !strcmp(path, parent)) crash_at("before-directory-sync");
    int result = real(fd);
    if (!result && journal_seen && !strcmp(path, proof)) crash_at("after-database-sync");
    return result;
}

int fsync(int fd) { return sync_boundary(fd, "fsync"); }
int fdatasync(int fd) { return sync_boundary(fd, "fdatasync"); }

int unlink(const char *path) {
    int (*real)(const char*) = dlsym(RTLD_NEXT, "unlink");
    const char *proof = getenv("PROOF_CRASH_FILE");
    char journal[PATH_MAX];
    if (proof) snprintf(journal, sizeof(journal), "%s-journal", proof);
    int result = real(path);
    if (!result && proof && journal_seen && !strcmp(path, journal))
        crash_at("after-journal-remove");
    if (!result && proof && !strncmp(path, proof, strlen(proof)) &&
        strstr(path + strlen(proof), ".initializing-") &&
        !strstr(path, "-journal") && !access(proof, F_OK)) crash_at("after-schema-unlink");
    return result;
}

int link(const char *source, const char *destination) {
    int (*real)(const char*, const char*) = dlsym(RTLD_NEXT, "link");
    int result = real(source, destination);
    const char *proof = getenv("PROOF_CRASH_FILE");
    if (!result && proof && !strcmp(destination, proof)) crash_at("after-schema-publish");
    return result;
}
