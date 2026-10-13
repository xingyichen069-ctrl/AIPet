"""Shared, read-only process checks for local management tools."""
import csv
import os
import subprocess


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0 and any(
            len(row) > 1 and row[1] == str(pid)
            for row in csv.reader((result.stdout or "").splitlines()))
    except (subprocess.SubprocessError, OSError):
        return False
