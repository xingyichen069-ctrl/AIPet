#include <windows.h>
#include <wchar.h>

static int fail_message(const wchar_t *message) {
    MessageBoxW(NULL, message, L"AIPet", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line, int show) {
    (void)instance;
    (void)previous;
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
    int cli = command_line && *command_line;
    HANDLE std_in = GetStdHandle(STD_INPUT_HANDLE);
    HANDLE std_out = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE std_err = GetStdHandle(STD_ERROR_HANDLE);
    if (cli && (std_out == NULL || std_out == INVALID_HANDLE_VALUE ||
                std_err == NULL || std_err == INVALID_HANDLE_VALUE) &&
        GetConsoleWindow() == NULL) {
        if (!AttachConsole(ATTACH_PARENT_PROCESS)) {
            AllocConsole();
        }
        std_in = GetStdHandle(STD_INPUT_HANDLE);
        std_out = GetStdHandle(STD_OUTPUT_HANDLE);
        std_err = GetStdHandle(STD_ERROR_HANDLE);
    }
    if (swprintf_s(python, sizeof(python) / sizeof(python[0]),
                   L"%s\\runtime\\%s", root, cli ? L"python.exe" : L"pythonw.exe") < 0 ||
        swprintf_s(script, sizeof(script) / sizeof(script[0]),
                   L"%s\\src\\windows_launcher.py", root) < 0 ||
        swprintf_s(command, sizeof(command) / sizeof(command[0]),
                   L"\"%s\" \"%s\"%s%s", python, script,
                   cli ? L" " : L"", cli ? command_line : L"") < 0) {
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
    DWORD creation_flags = CREATE_UNICODE_ENVIRONMENT;
    BOOL inherit_handles = FALSE;
    if (!cli) {
        creation_flags |= CREATE_NO_WINDOW;
    } else {
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = std_in;
        startup.hStdOutput = std_out;
        startup.hStdError = std_err;
        if (std_in != NULL && std_in != INVALID_HANDLE_VALUE) {
            SetHandleInformation(std_in, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        }
        if (std_out != NULL && std_out != INVALID_HANDLE_VALUE) {
            SetHandleInformation(std_out, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        }
        if (std_err != NULL && std_err != INVALID_HANDLE_VALUE) {
            SetHandleInformation(std_err, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        }
        inherit_handles = TRUE;
    }
    if (!CreateProcessW(python, command, NULL, NULL, inherit_handles,
                        creation_flags,
                        NULL, root, &startup, &process)) {
        return fail_message(L"AIPet failed to start. Check data\\cache\\windows-startup.log.");
    }
    CloseHandle(process.hThread);
    if (cli) {
        DWORD exit_code = 1;
        WaitForSingleObject(process.hProcess, INFINITE);
        GetExitCodeProcess(process.hProcess, &exit_code);
        CloseHandle(process.hProcess);
        return (int)exit_code;
    }
    CloseHandle(process.hProcess);
    return 0;
}
