"""`~/.arkagent/` directory layout.

One frozen value object owns every on-disk location so no module builds paths by
hand. Tests pass an override to get an isolated root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArkAgentPaths:
    root: Path

    @classmethod
    def from_env(cls, override: str | os.PathLike[str] | None = None) -> "ArkAgentPaths":
        """Resolve the data root. Precedence: explicit override → `$ARKAGENT_HOME`
        → `~/.arkagent`."""
        if override is not None:
            root = Path(override)
        elif os.environ.get("ARKAGENT_HOME"):
            root = Path(os.environ["ARKAGENT_HOME"])
        else:
            root = Path.home() / ".arkagent"
        return cls(root=root.expanduser().resolve())

    # ── derived paths ────────────────────────────────────────────────────────
    @property
    def config(self) -> Path:
        return self.root / "config.yaml"

    def ensure(self) -> "ArkAgentPaths":
        """Create the directory skeleton if missing. Idempotent."""
        self.root.mkdir(parents=True, exist_ok=True)
        return self
