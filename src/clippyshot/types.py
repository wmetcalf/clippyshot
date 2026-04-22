"""Shared dataclasses used across components."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DetectionSource = Literal["magika", "extension"]


@dataclass(frozen=True)
class DetectedType:
    """Result of running the detector on an input file."""
    label: str
    mime: str
    extension_hint: str
    confidence: float
    source: DetectionSource
    agreed_with_extension: bool
    warnings: list[str] = None
    magika_label: str = ""       # raw label from Magika before correction
    magika_mime: str = ""        # raw MIME from Magika
    libmagic_mime: str = ""      # MIME from libmagic (if available)

    def __post_init__(self):
        if self.warnings is None:
            object.__setattr__(self, "warnings", [])


@dataclass(frozen=True)
class PageHashes:
    phash: str
    colorhash: str
    sha256: str
    is_blank: bool = False

    def to_dict(self) -> dict:
        return {
            "phash": self.phash,
            "colorhash": self.colorhash,
            "sha256": self.sha256,
            "is_blank": self.is_blank,
        }


@dataclass(frozen=True)
class RasterizedPage:
    index: int
    path: str
    width_px: int
    height_px: int
    width_mm: float
    height_mm: float


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    killed: bool

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.killed
