"""Local persona library with public defaults and private editable copies."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


class PersonaManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.defaults_dir = self.root / "persona_defaults"
        self.persona_dir = self.root / "persona"
        self.characters_dir = self.persona_dir / "characters"
        self.active_file = self.persona_dir / "active.json"
        self.legacy_soul = self.persona_dir / "SOUL.md"
        self.legacy_boundaries = self.persona_dir / "BOUNDARIES.md"
        self.ensure_initialized()

    @staticmethod
    def slug(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", value.strip().lower()).strip("-")
        return value or "persona"

    @staticmethod
    def _display_name(folder: Path, soul: Path) -> str:
        try:
            for line in soul.read_text(encoding="utf-8").splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    if title:
                        return title.replace("的人格", "")
        except OSError:
            pass
        return folder.name

    def _default_ids(self) -> list[str]:
        if not self.defaults_dir.exists():
            return []
        return sorted(p.name for p in self.defaults_dir.iterdir() if p.is_dir() and (p / "SOUL.md").exists())

    def ensure_initialized(self) -> None:
        self.characters_dir.mkdir(parents=True, exist_ok=True)
        defaults = self._default_ids()
        # The old single-file persona remains the source of truth for Hiyori on first run.
        hiyori = self.characters_dir / "hiyori"
        if not (hiyori / "SOUL.md").exists() and self.legacy_soul.exists():
            hiyori.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.legacy_soul, hiyori / "SOUL.md")
            if self.legacy_boundaries.exists():
                shutil.copyfile(self.legacy_boundaries, hiyori / "BOUNDARIES.md")
        for pid in defaults:
            target = self.characters_dir / pid
            target.mkdir(parents=True, exist_ok=True)
            source = self.defaults_dir / pid
            for name in ("SOUL.md", "BOUNDARIES.md", "DIALOGUE.json", "MOODS.json", "avatar.png"):
                src = source / name
                dst = target / name
                if name == "MOODS.json" and pid == "hiyori" and not dst.exists():
                    legacy_moods = self.root / "data/mood_catalog.json"
                    if legacy_moods.is_file():
                        shutil.copyfile(legacy_moods, dst)
                        continue
                if src.exists() and not dst.exists():
                    shutil.copyfile(src, dst)
        if not self.active_file.exists():
            active = "hiyori" if (hiyori / "SOUL.md").exists() else (defaults[0] if defaults else "hiyori")
            self._write_active(active)
        else:
            try:
                active = json.loads(self.active_file.read_text(encoding="utf-8")).get("id")
            except (OSError, ValueError, TypeError):
                active = None
            if not active or not (self.characters_dir / str(active) / "SOUL.md").exists():
                fallback = "hiyori" if (hiyori / "SOUL.md").exists() else (defaults[0] if defaults else "hiyori")
                self._write_active(fallback)

    def _write_active(self, pid: str) -> None:
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        self.active_file.write_text(json.dumps({"id": pid}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def active_id(self) -> str:
        try:
            return str(json.loads(self.active_file.read_text(encoding="utf-8")).get("id", "hiyori"))
        except (OSError, ValueError, TypeError):
            return "hiyori"

    def list_personas(self) -> list[dict]:
        items = []
        for folder in sorted(self.characters_dir.iterdir()) if self.characters_dir.exists() else []:
            soul = folder / "SOUL.md"
            if not folder.is_dir() or not soul.exists():
                continue
            items.append({
                "id": folder.name,
                "name": self._display_name(folder, soul),
                "path": str(folder),
                "soul": str(soul),
                "avatar": str(folder / "avatar.png") if (folder / "avatar.png").exists() else "",
                "active": folder.name == self.active_id(),
            })
        return items

    def active(self) -> dict:
        pid = self.active_id()
        for item in self.list_personas():
            if item["id"] == pid:
                return item
        return self.list_personas()[0] if self.list_personas() else {
            "id": pid, "name": pid, "path": str(self.characters_dir / pid),
            "soul": str(self.characters_dir / pid / "SOUL.md"), "avatar": "", "active": True,
        }

    def active_files(self) -> dict[str, Path]:
        item = self.active()
        folder = Path(item["path"])
        files = {"SOUL.md": folder / "SOUL.md"}
        boundary = folder / "BOUNDARIES.md"
        if boundary.exists():
            files["BOUNDARIES.md"] = boundary
        return files

    def mood_binding(self, pid: str) -> str:
        import persona_runtime as PR
        return PR.mood_profile(self.root, self.slug(pid))

    def mood_profiles(self) -> list[str]:
        import persona_runtime as PR
        ids = {p.name for base in (self.defaults_dir, self.characters_dir)
               if base.exists() for p in base.iterdir() if p.is_dir() and PR.valid_id(p.name)}
        return sorted(ids)

    def set_mood_profile(self, pid: str, profile: str) -> None:
        import persona_runtime as PR
        pid, profile = self.slug(pid), self.slug(profile)
        if profile not in self.mood_profiles():
            raise ValueError("找不到这个情绪方案。")
        path = self.root / "data/persona_moods.json"
        data = PR.read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        data[pid] = profile
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def read_moods(self, profile: str) -> str:
        import persona_runtime as PR
        path = PR.files(self.root, self.slug(profile)).get("MOODS.json")
        return path.read_text(encoding="utf-8") if path else "{}"

    def save_moods(self, pid: str, text: str) -> None:
        data = json.loads(text)
        if not isinstance(data, dict) or not data:
            raise ValueError("情绪方案需要至少一个状态。")
        for key, row in data.items():
            if (not isinstance(key, str) or not isinstance(row, dict)
                    or not isinstance(row.get("voice"), str)
                    or not isinstance(row.get("hours", 2), (int, float))
                    or not 0 < row.get("hours", 2) <= 24
                    or not all(isinstance(row.get(f, ""), str) for f in ("feel", "avoid"))
                    or not isinstance(row.get("sample", []), list)
                    or not all(isinstance(x, str) for x in row.get("sample", []))):
                raise ValueError("状态需要 voice 文案、有效的 hours 时长和文本示例列表。")
        folder = self.characters_dir / self.slug(pid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "MOODS.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def set_active(self, pid: str) -> dict:
        pid = self.slug(pid)
        soul = self.characters_dir / pid / "SOUL.md"
        if not soul.exists():
            raise ValueError("找不到这个人格。")
        self._write_active(pid)
        return self.active()

    def read_soul(self, pid: str | None = None) -> str:
        folder = self.characters_dir / self.slug(pid or self.active_id())
        path = folder / "SOUL.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def save_soul(self, pid: str, text: str, name: str | None = None) -> dict:
        pid = self.slug(pid)
        folder = self.characters_dir / pid
        folder.mkdir(parents=True, exist_ok=True)
        text = text.strip() + "\n"
        if not text.strip():
            raise ValueError("人格内容不能为空。")
        if name and not text.lstrip().startswith("#"):
            text = f"# {name.strip()}\n\n{text}"
        (folder / "SOUL.md").write_text(text, encoding="utf-8")
        return self.active() if pid == self.active_id() else next(x for x in self.list_personas() if x["id"] == pid)

    def create(self, name: str, soul: str = "") -> dict:
        pid = self.slug(name)
        if (self.characters_dir / pid / "SOUL.md").exists():
            raise ValueError("这个人格已经存在。")
        if not soul.strip():
            soul = f"# {name}\n\n## 说话方式\n- 温和、自然，先理解再回应。\n"
        return self.save_soul(pid, soul, name=name)

    def import_soul(self, source: str | Path, pid: str | None = None) -> dict:
        source = Path(source)
        text = source.read_text(encoding="utf-8")
        target = self.slug(pid or source.stem)
        if target == "soul":
            target = "imported-persona"
        return self.save_soul(target, text, name=source.stem)

    def import_avatar(self, source: str | Path, pid: str | None = None) -> str:
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        target_id = self.slug(pid or self.active_id())
        folder = self.characters_dir / target_id
        if not (folder / "SOUL.md").exists():
            raise ValueError("请先选择一个已有的人格。")
        target = folder / "avatar.png"
        shutil.copyfile(source, target)
        return str(target)
