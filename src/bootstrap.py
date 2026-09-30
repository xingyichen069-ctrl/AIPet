"""First-run files and local validation; no GUI, network or persona imports."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

TEMPLATES = ("config", "thinking", "secrets")


class ConfigError(ValueError):
    """An actionable error which never includes credential values."""


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} 第 {exc.lineno} 行、第 {exc.colno} 列格式不对。"
                          "请检查英文双引号、逗号和括号；不要加 // 注释。") from None
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取 {path.name}，请检查文件是否存在、权限及 UTF-8 编码。") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path.name} 最外层应是 {{ ... }}，不能是列表或文字。")
    return value


def initialize(root: Path) -> list[str]:
    """Create missing local files exclusively; never rewrite an existing file.

    Persona initialization belongs to the first application launch, after any
    old-install migration. Creating it here would shadow legacy SOUL.md files.
    """
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    created = []
    for name in TEMPLATES:
        target = data / f"{name}.json"
        if target.exists():
            continue
        example = data / f"{name}.example.json"
        read_object(example)
        content = example.read_bytes()
        try:
            # O_EXCL also prevents two first launches overwriting each other.
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        created.append(str(target.relative_to(root)))
    return created


def merge_defaults(defaults: dict, local: dict) -> dict:
    """Fill missing versioned defaults in memory, preserving the file on disk."""
    result = deepcopy(defaults)
    for key, value in local.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_defaults(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(root: Path) -> dict:
    initialize(root)
    data = root / "data"
    return merge_defaults(read_object(data / "config.example.json"),
                          read_object(data / "config.json"))


def is_unedited_template(root: Path, relative: str) -> bool:
    path = root / relative
    if path.parent != root / "data" or path.stem not in TEMPLATES:
        return False
    example = path.with_name(f"{path.stem}.example.json")
    try:
        return read_object(path) == read_object(example)
    except ConfigError:
        return False


def validate(root: Path) -> list[str]:
    """Return local format/type errors, without sending credentials anywhere."""
    errors = []
    values = {}
    for name in TEMPLATES:
        try:
            values[name] = read_object(root / "data" / f"{name}.json")
        except ConfigError as exc:
            errors.append(str(exc))
    if "secrets" in values:
        secrets = values["secrets"]
        for key in ("deepseek_api_key", "deepseek_base_url", "auth_token", "base_url",
                    "model", "vision_api_key", "vision_base_url", "vision_model"):
            if key in secrets and not isinstance(secrets[key], str):
                errors.append(f"secrets.json 的 {key} 应放在英文双引号内。")
        for key in ("deepseek_base_url", "base_url", "vision_base_url"):
            value = secrets.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    parsed = urlsplit(value.strip())
                    valid = parsed.scheme in ("http", "https") and bool(parsed.netloc)
                except ValueError:
                    valid = False
                if not valid or value.rstrip("/").endswith("/chat/completions"):
                    errors.append(f"secrets.json 的 {key} 应是 http(s) 接口根地址，"
                                  "不要包含 /chat/completions。")
    if "config" in values:
        try:
            config = merge_defaults(read_object(root / "data/config.example.json"), values["config"])
            for section in ("paths", "retrieval", "compression", "privacy", "tools", "knowledge", "live2d"):
                if not isinstance(config.get(section), dict):
                    errors.append(f"config.json 的 {section} 应是 {{ ... }}。")
            paths = config.get("paths")
            if isinstance(paths, dict):
                for key in ("journal", "state", "profile", "summaries", "archive", "view"):
                    if not isinstance(paths.get(key), str) or not paths[key].strip():
                        errors.append(f"config.json 的 paths.{key} 应是非空路径。")
            privacy = config.get("privacy")
            if isinstance(privacy, dict):
                patterns = privacy.get("blocked_patterns")
                if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                    errors.append("config.json 的 privacy.blocked_patterns 应是文字列表。")
        except ConfigError as exc:
            errors.append(str(exc))
    if "thinking" in values:
        cfg = values["thinking"]
        presets = cfg.get("presets")
        if not isinstance(presets, dict) or not presets:
            errors.append("thinking.json 缺少 presets 档位表；请参考 thinking.example.json，勿用局部示例替换整份文件。")
        else:
            current = cfg.get("current")
            if not isinstance(current, str) or (current != "auto" and current not in presets):
                errors.append("thinking.json 的 current 不是现有档位，也不是 auto。")
                current = "daily"
            needed = {"daily", "frugal", "serious", "deep", "max"} if current == "auto" else {"daily", current}
            for name in needed:
                params = presets.get(name, {}).get("params") if isinstance(presets.get(name), dict) else None
                if not isinstance(params, dict):
                    errors.append(f"thinking.json 缺少 {name} 的 params 参数。")
                    continue
                for key in ("memory_budget", "memory_entries", "max_tokens"):
                    value = params.get(key)
                    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                        errors.append(f"thinking.json 的 {name}.{key} 应是正整数。")
    return errors
