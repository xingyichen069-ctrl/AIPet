"""Installation boundaries, initially pointing at the existing directory layout."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    install: Path

    @property
    def code(self) -> Path:
        return self.install / "src"

    @property
    def userdata(self) -> Path:
        # Keep legacy data/persona/memory paths in place; no implicit migration.
        return self.install

    @property
    def runtime(self) -> Path:
        return self.install / "runtime"

    @property
    def data(self) -> Path:
        return self.userdata / "data"

    @property
    def portable(self) -> bool:
        return ((self.install / "release-manifest.json").is_file()
                or ((self.install / "AIPet.exe").is_file()
                    and (self.runtime / "python.exe").is_file()))


PATHS = AppPaths(Path(__file__).resolve().parent.parent)
