"""Isolated plugin discovery, testing, packaging and activation.

The harness deliberately has no dependency on Qt, AIPet runtime data, or a
network client.  A plugin is a directory with a strict manifest.  The source
tree is tested first, then packaged into a content-addressed archive, staged,
health-checked and activated by an atomic registry update.  Existing active
versions are never overwritten, so a failed activation leaves the previous
version usable.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable


SCHEMA_VERSION = 1
RELEASE_MANIFEST = "release-manifest.json"
PLUGIN_MANIFEST = ".aipet-plugin/plugin.json"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


class HarnessError(RuntimeError):
    """A user-facing, non-secret harness failure."""


def _safe_relative(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise HarnessError(f"插件路径无效：{raw!r}")
    value = raw.replace("\\", "/")
    path = Path(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:/", value):
        raise HarnessError(f"插件路径不能是绝对路径：{raw!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HarnessError(f"插件路径越界：{raw!r}")
    if any(ord(char) < 32 for char in value):
        raise HarnessError(f"插件路径含控制字符：{raw!r}")
    return "/".join(parts)


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise HarnessError(f"插件路径越过根目录：{relative}")
    return path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HarnessError(f"无法读取 JSON：{path.name}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"JSON 根节点不是对象：{path.name}")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _iter_regular_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            # Interpreter by-products are never part of a plugin release.
            continue
        if path.is_symlink():
            raise HarnessError(f"插件不能包含符号链接：{path.relative_to(root)}")
        if not path.is_file():
            raise HarnessError(f"插件包含非普通文件：{path.relative_to(root)}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise HarnessError(f"插件文件过大：{path.relative_to(root)}")
        yield path


@dataclass(frozen=True)
class PluginSpec:
    root: Path
    name: str
    version: str
    display_name: str
    entrypoint: str
    files: tuple[str, ...]
    test_command: tuple[str, ...]
    health_command: tuple[str, ...]


def _command(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise HarnessError(f"插件 {field} 必须是非空字符串数组")
    return tuple(value)


def validate_plugin(plugin_root: Path, *, allow_release_manifest: bool = False) -> PluginSpec:
    """Validate a source plugin without executing its code."""
    root = Path(plugin_root).resolve()
    if not root.is_dir():
        raise HarnessError(f"插件目录不存在：{root}")
    manifest_path = root / PLUGIN_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise HarnessError("插件缺少 .aipet-plugin/plugin.json")
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError("插件清单版本不兼容")
    name = manifest.get("name")
    version = manifest.get("version")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise HarnessError("插件名称必须是小写安全标识符")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise HarnessError("插件版本必须是三段数字版本")
    display_name = manifest.get("display_name", name)
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 120:
        raise HarnessError("插件 display_name 无效")
    entrypoint = _safe_relative(manifest.get("entrypoint"))
    if not entrypoint.endswith(".py"):
        raise HarnessError("插件入口必须是 Python 文件")
    files_raw = manifest.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise HarnessError("插件 files 必须是非空数组")
    files = tuple(_safe_relative(item) for item in files_raw)
    if len(set(files)) != len(files):
        raise HarnessError("插件 files 不能重复")
    if PLUGIN_MANIFEST not in files or entrypoint not in files:
        raise HarnessError("插件 files 必须包含清单和入口")
    for relative in files:
        path = _safe_path(root, relative)
        if not path.is_file() or path.is_symlink():
            raise HarnessError(f"插件文件不存在或为链接：{relative}")
    actual = {path.relative_to(root).as_posix() for path in _iter_regular_files(root)}
    if allow_release_manifest:
        actual.discard(RELEASE_MANIFEST)
    if actual != set(files):
        extra = sorted(actual - set(files))
        missing = sorted(set(files) - actual)
        raise HarnessError(f"插件文件清单不一致；多余={extra[:3]} 缺少={missing[:3]}")
    return PluginSpec(
        root=root,
        name=name,
        version=version,
        display_name=display_name.strip(),
        entrypoint=entrypoint,
        files=files,
        test_command=_command(manifest.get("test_command"), "test_command"),
        health_command=_command(manifest.get("health_command"), "health_command"),
    )


def discover_plugins(plugins_root: Path) -> list[PluginSpec]:
    root = Path(plugins_root).resolve()
    if not root.is_dir():
        return []
    result = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / PLUGIN_MANIFEST).is_file():
            result.append(validate_plugin(child))
    return result


def _command_args(command: tuple[str, ...], spec: PluginSpec, app_root: Path) -> list[str]:
    values = {
        "{python}": sys.executable,
        "{root}": str(spec.root),
        "{app_root}": str(app_root.resolve()),
        "{entrypoint}": str((spec.root / spec.entrypoint).resolve()),
    }
    return [values.get(item, item) for item in command]


def _run_command(spec: PluginSpec, command: tuple[str, ...], app_root: Path, *, timeout: int) -> dict[str, Any]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "AIPET_ROOT": str(Path(app_root).resolve()),
        "AIPET_HARNESS_PLUGIN_ROOT": str(spec.root),
        "AIPET_HARNESS_TEST": "1",
    }
    try:
        result = subprocess.run(
            _command_args(command, spec, Path(app_root)),
            cwd=spec.root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"插件命令未完成：{spec.name}") from exc
    output = (result.stdout or "")[-4000:]
    error = (result.stderr or "")[-2000:]
    if result.returncode:
        # A plugin is third-party code.  Do not echo its arbitrary stderr into
        # the host UI; opt into the bounded tail only while developing it.
        if os.environ.get("AIPET_HARNESS_DEBUG") == "1":
            raise HarnessError(f"插件命令失败：{spec.name} exit={result.returncode}\n{error or output}")
        raise HarnessError(f"插件命令失败：{spec.name} exit={result.returncode}")
    return {"returncode": result.returncode, "stdout": output, "stderr": error}


def run_plugin_test(spec: PluginSpec, app_root: Path) -> dict[str, Any]:
    """Run the manifest test command with a clean, bounded environment."""
    return _run_command(spec, spec.test_command, Path(app_root), timeout=90)


def _health_check(spec: PluginSpec, app_root: Path) -> dict[str, Any]:
    return _run_command(spec, spec.health_command, Path(app_root), timeout=30)


def _release_manifest(spec: PluginSpec) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": spec.name,
        "version": spec.version,
        "files": [
            {"path": name, "size": (spec.root / name).stat().st_size, "sha256": _digest(spec.root / name)}
            for name in spec.files
        ],
    }


def package_plugin(spec: PluginSpec, output_dir: Path) -> Path:
    """Create and verify a deterministic-ish plugin archive."""
    target_dir = Path(output_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / f"{spec.name}-{spec.version}.zip"
    release = _release_manifest(spec)
    root_name = f"{spec.name}-{spec.version}"
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in spec.files:
                info = zipfile.ZipInfo(f"{root_name}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                zf.writestr(info, (spec.root / name).read_bytes())
            info = zipfile.ZipInfo(f"{root_name}/{RELEASE_MANIFEST}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        archive.unlink(missing_ok=True)
        raise HarnessError(f"插件打包失败：{spec.name}") from exc
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        archive.unlink(missing_ok=True)
        raise HarnessError("插件压缩包过大")
    _verify_archive(archive)
    return archive


def _verify_archive(archive: Path) -> tuple[Path, dict[str, Any]]:
    archive = Path(archive).resolve()
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise HarnessError("插件压缩包不存在或过大")
    with zipfile.ZipFile(archive) as zf:
        entries = zf.infolist()
        if not entries:
            raise HarnessError("插件压缩包为空")
        names = []
        total = 0
        for info in entries:
            name = _safe_relative(info.filename.rstrip("/"))
            if name in names or info.is_dir():
                raise HarnessError("插件压缩包含重复或目录条目")
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                raise HarnessError("插件压缩包不能包含符号链接")
            names.append(name)
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise HarnessError("插件解压体积过大")
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1:
            raise HarnessError("插件压缩包必须只有一个根目录")
        root_name = next(iter(roots))
        release_name = f"{root_name}/{RELEASE_MANIFEST}"
        if release_name not in names:
            raise HarnessError("插件压缩包缺少发布清单")
        try:
            release = json.loads(zf.read(release_name).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise HarnessError("插件发布清单无效") from exc
        if not isinstance(release, dict) or release.get("schema_version") != SCHEMA_VERSION:
            raise HarnessError("插件发布清单版本不兼容")
        files = release.get("files")
        if not isinstance(files, list) or not files:
            raise HarnessError("插件发布清单缺少文件")
        for item in files:
            if not isinstance(item, dict):
                raise HarnessError("插件发布清单含无效文件")
            relative = _safe_relative(item.get("path"))
            member = f"{root_name}/{relative}"
            if member not in names or type(item.get("size")) is not int or item["size"] < 0:
                raise HarnessError(f"插件发布清单缺少文件：{relative}")
            if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
                raise HarnessError(f"插件发布清单缺少哈希：{relative}")
            data = zf.read(member)
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise HarnessError(f"插件压缩包哈希校验失败：{relative}")
        if set(names) != {f"{root_name}/{item['path']}" for item in files} | {release_name}:
            raise HarnessError("插件压缩包与发布清单不一致")
        manifest_member = f"{root_name}/{PLUGIN_MANIFEST}"
        if manifest_member not in names:
            raise HarnessError("插件压缩包缺少插件清单")
        temp = Path(tempfile.mkdtemp(prefix="aipet-plugin-verify-"))
        try:
            zf.extractall(temp)
            extracted_root = temp / root_name
            # release-manifest.json belongs to the archive envelope, not to
            # the plugin source manifest.  Remove only this freshly extracted
            # copy before applying the exact source-file check.
            (extracted_root / RELEASE_MANIFEST).unlink(missing_ok=True)
            spec = validate_plugin(extracted_root)
            if spec.name != release.get("name") or spec.version != release.get("version"):
                raise HarnessError("插件清单与发布清单不一致")
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        return Path(root_name), release


def _extract_archive(archive: Path, destination: Path) -> Path:
    archive = Path(archive).resolve()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        names = [_safe_relative(info.filename.rstrip("/")) for info in zf.infolist() if not info.is_dir()]
        root_names = {name.split("/", 1)[0] for name in names}
        if len(root_names) != 1:
            raise HarnessError("插件压缩包根目录不唯一")
        root_name = next(iter(root_names))
        for info in zf.infolist():
            raw = info.filename.rstrip("/")
            if info.is_dir():
                continue
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                raise HarnessError("插件压缩包不能包含符号链接")
            relative = _safe_relative(raw)
            target = _safe_path(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise HarnessError("插件暂存路径重复")
            with zf.open(info) as source, target.open("xb") as out:
                shutil.copyfileobj(source, out, length=1024 * 1024)
    return destination / root_name


def _state_paths(app_root: Path) -> tuple[Path, Path, Path]:
    state = Path(app_root).resolve() / "data" / "harness"
    return state, state / "plugins", state / "registry.json"


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "active": {}}
    value = _read_json(path)
    if value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("active"), dict):
        raise HarnessError("插件注册表版本不兼容")
    return value


def install_archive(archive: Path, app_root: Path) -> dict[str, Any]:
    """Stage, health-check and activate an archive without replacing old versions."""
    _verify_archive(archive)
    archive_digest = _digest(Path(archive))
    state, plugins_dir, registry_path = _state_paths(Path(app_root))
    staging = state / "staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    try:
        extracted = _extract_archive(Path(archive), staging)
        spec = validate_plugin(extracted, allow_release_manifest=True)
        registry = _load_registry(registry_path)
        current = registry["active"].get(spec.name)
        version_dir = plugins_dir / spec.name / spec.version
        if isinstance(current, dict) and current.get("version") == spec.version:
            if not version_dir.is_dir() or version_dir.is_symlink():
                raise HarnessError("注册表指向的插件版本不存在")
            if current.get("archive_sha256") not in {None, archive_digest}:
                raise HarnessError("同一插件版本内容不同，请递增版本后再启用")
            if current.get("archive_sha256") is None:
                for name in spec.files:
                    if _digest(version_dir / name) != _digest(extracted / name):
                        raise HarnessError("同一插件版本内容不同，请递增版本后再启用")
                current = dict(current)
                current["archive_sha256"] = archive_digest
                registry["active"][spec.name] = current
                _write_json_atomic(registry_path, registry)
            return {"status": "already_active", "name": spec.name, "version": spec.version,
                    "path": current.get("path", ""), "archive_sha256": archive_digest}
        _health_check(spec, Path(app_root))
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        if version_dir.exists() or version_dir.is_symlink():
            raise HarnessError("目标插件版本已存在但未激活，需人工复核")
        temporary = version_dir.parent / f".{version_dir.name}.{uuid.uuid4().hex}.tmp"
        shutil.copytree(extracted, temporary)
        try:
            os.replace(temporary, version_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        relative = version_dir.relative_to(Path(app_root).resolve()).as_posix()
        registry["active"][spec.name] = {"version": spec.version, "path": relative, "enabled": True,
                                          "archive_sha256": archive_digest,
                                          "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            _write_json_atomic(registry_path, registry)
        except Exception:
            # The new version is disposable until the registry points at it.
            # Keep the previous active version and remove only this candidate.
            shutil.rmtree(version_dir, ignore_errors=True)
            raise
        return {"status": "activated", "name": spec.name, "version": spec.version, "path": relative,
                "previous": current}
    except Exception:
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def run_plugin(app_root: Path, name: str, args: list[str] | None = None) -> int:
    state, _, registry_path = _state_paths(Path(app_root))
    del state
    registry = _load_registry(registry_path)
    active = registry["active"].get(name)
    if not isinstance(active, dict) or not active.get("enabled"):
        raise HarnessError(f"插件未启用：{name}")
    root = _safe_path(Path(app_root).resolve(), str(active.get("path", "")))
    spec = validate_plugin(root, allow_release_manifest=True)
    command = [*_command_args(("{python}", "-B", "{entrypoint}"), spec, Path(app_root)), *(args or [])]
    env = dict(os.environ)
    env.update(AIPET_ROOT=str(Path(app_root).resolve()), AIPET_HARNESS_PLUGIN_ROOT=str(root))
    return subprocess.call(command, cwd=root, env=env)


def auto_use(plugin_root: Path, app_root: Path, package_dir: Path | None = None) -> dict[str, Any]:
    """Test, package and activate one plugin in one bounded operation."""
    spec = validate_plugin(Path(plugin_root))
    tested = run_plugin_test(spec, Path(app_root))
    output = Path(package_dir) if package_dir else Path(app_root).resolve() / "work" / "harness-packages"
    archive = package_plugin(spec, output)
    installed = install_archive(archive, Path(app_root))
    return {"plugin": spec.name, "version": spec.version, "tested": True,
            "archive": str(archive), "archive_sha256": _digest(archive), "installation": installed,
            "test_stdout": tested["stdout"][-1000:]}
