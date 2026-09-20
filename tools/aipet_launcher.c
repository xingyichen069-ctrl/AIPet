#include <windows.h>
#include <wchar.h>

static int fail_message(const wchar_t *message) {
    MessageBoxW(NULL, message, L"AIPet", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line, int show) {
    (void)instance;
    (void)previous;
    (void)command_line;
    (void)show;

    wchar_t root[32768];
    DWORD length = GetModuleFileNameW(NULL, root, (DWORD)(sizeof(root) / sizeof(root[0])));
    if (!length || length >= (DWORD)(sizeof(root) / sizeof(root[0]))) {
        return fail_message(L"Cannot locate the AIPet folder.");
    }
    wchar_t *slash = wcsrchr(root, L'\\');
    if (!slash) {
        return fail_message(L"Cannot locate the AIPet folder.");
    }
    *slash = L'\0';

    wchar_t python[32768];
    wchar_t script[32768];
    wchar_t command[65536];
    if (swprintf_s(python, sizeof(python) / sizeof(python[0]),
                   L"%s\\runtime\\pythonw.exe", root) < 0 ||
        swprintf_s(script, sizeof(script) / sizeof(script[0]),
                   L"%s\\src\\windows_launcher.py", root) < 0 ||
        swprintf_s(command, sizeof(command) / sizeof(command[0]),
                   L"\"%s\" \"%s\"", python, script) < 0) {
        return fail_message(L"The AIPet path is too long.");
    }
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        return fail_message(L"The bundled runtime is incomplete. Re-extract AIPet.");
    }

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                        NULL, root, &startup, &process)) {
        return fail_message(L"AIPet failed to start. Check data\\cache\\windows-startup.log.");
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
