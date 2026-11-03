/* Windows console launcher. The shell waits on this exe, not on cmd.exe.
 *
 * It reads grok.launch (UTF-8, one argument per line, next to this exe),
 * appends its own command-line arguments, and runs that process in this
 * console. Ctrl+C is ignored here and left for the child. Build with:
 *   gcc -O2 -s -o grok_shim.exe grok_shim.c -lshell32
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

#define MAX_ARGS 32
#define MAX_LINE 4096
#define MAX_CMD 32768

static BOOL WINAPI on_ctrl(DWORD kind) {
    if (kind == CTRL_C_EVENT || kind == CTRL_BREAK_EVENT)
        return TRUE;
    return FALSE;
}

static wchar_t *quote_arg(const wchar_t *arg, wchar_t *dst, wchar_t *end) {
    const wchar_t *p;
    int quote = (*arg == L'\0');
    int backslashes = 0;
    for (p = arg; *p; p++) {
        if (*p == L' ' || *p == L'\t' || *p == L'"')
            quote = 1;
    }
    if (!quote) {
        while (*arg && dst < end - 1)
            *dst++ = *arg++;
        return dst;
    }
    if (dst >= end - 1)
        return NULL;
    *dst++ = L'"';
    for (p = arg; *p; p++) {
        if (*p == L'\\') {
            backslashes++;
            continue;
        }
        if (*p == L'"') {
            int i;
            for (i = 0; i < backslashes * 2 + 1; i++) {
                if (dst >= end - 1)
                    return NULL;
                *dst++ = L'\\';
            }
            backslashes = 0;
        } else {
            while (backslashes-- > 0) {
                if (dst >= end - 1)
                    return NULL;
                *dst++ = L'\\';
            }
            backslashes = 0;
        }
        if (dst >= end - 1)
            return NULL;
        *dst++ = *p;
    }
    while (backslashes-- > 0) {
        if (dst >= end - 2)
            return NULL;
        *dst++ = L'\\';
    }
    if (dst >= end - 1)
        return NULL;
    *dst++ = L'"';
    return dst;
}

static int load_launch(const wchar_t *path, wchar_t args[][MAX_LINE], int maxn) {
    FILE *fp;
    char raw[64 * 1024];
    wchar_t wide[64 * 1024];
    int nread, nwide, count = 0, i = 0;
    fp = _wfopen(path, L"rb");
    if (!fp)
        return -1;
    nread = (int)fread(raw, 1, sizeof raw - 1, fp);
    fclose(fp);
    if (nread < 0)
        return -1;
    raw[nread] = 0;
    if (nread >= 3 && (unsigned char)raw[0] == 0xEF
        && (unsigned char)raw[1] == 0xBB && (unsigned char)raw[2] == 0xBF) {
        memmove(raw, raw + 3, nread - 3);
        nread -= 3;
        raw[nread] = 0;
    }
    nwide = MultiByteToWideChar(CP_UTF8, 0, raw, nread, wide, 64 * 1024 - 1);
    if (nwide <= 0)
        return -1;
    wide[nwide] = 0;
    while (wide[i] && count < maxn) {
        int start = i, len;
        while (wide[i] && wide[i] != L'\n' && wide[i] != L'\r')
            i++;
        len = i - start;
        if (len >= MAX_LINE)
            len = MAX_LINE - 1;
        if (len > 0) {
            wmemcpy(args[count], wide + start, len);
            args[count][len] = 0;
            count++;
        }
        while (wide[i] == L'\n' || wide[i] == L'\r')
            i++;
    }
    return count;
}

int main(void) {
    wchar_t self[MAX_PATH];
    wchar_t launch_path[MAX_PATH];
    wchar_t stored[MAX_ARGS][MAX_LINE];
    wchar_t cmdline[MAX_CMD];
    wchar_t *dst;
    wchar_t **argv = NULL;
    int argc = 0, stored_n, i;
    DWORD len, code = 1;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;

    len = GetModuleFileNameW(NULL, self, MAX_PATH);
    if (len == 0 || len >= MAX_PATH)
        return 2;
    lstrcpynW(launch_path, self, MAX_PATH);
    {
        wchar_t *slash = wcsrchr(launch_path, L'\\');
        if (!slash)
            return 2;
        lstrcpynW(slash + 1, L"grok.launch", (int)(MAX_PATH - (slash + 1 - launch_path)));
    }
    stored_n = load_launch(launch_path, stored, MAX_ARGS);
    if (stored_n <= 0) {
        fwprintf(stderr, L"voyager grok launcher: cannot read %ls\n", launch_path);
        return 2;
    }

    dst = cmdline;
    for (i = 0; i < stored_n; i++) {
        if (i && dst < cmdline + MAX_CMD - 1)
            *dst++ = L' ';
        dst = quote_arg(stored[i], dst, cmdline + MAX_CMD);
        if (!dst)
            return 2;
    }
    argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv) {
        for (i = 1; i < argc; i++) {
            if (dst < cmdline + MAX_CMD - 1)
                *dst++ = L' ';
            dst = quote_arg(argv[i], dst, cmdline + MAX_CMD);
            if (!dst) {
                LocalFree(argv);
                return 2;
            }
        }
        LocalFree(argv);
    }
    *dst = 0;

    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(NULL, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fwprintf(stderr, L"voyager grok launcher: CreateProcess failed (%lu)\n", GetLastError());
        return 1;
    }
    /* After the child exists, so the child does not ignore Ctrl+C. */
    SetConsoleCtrlHandler(on_ctrl, TRUE);
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
