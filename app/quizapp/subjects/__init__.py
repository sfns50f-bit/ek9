"""科目の登録。科目を足すときはここに 1 行足す（中心部分は触らない）。"""

from __future__ import annotations

from .base import Subject
from .math import MathSubject

REGISTRY: dict[str, Subject] = {s.id: s for s in (MathSubject(),)}


def get_subject(subject_id: str) -> Subject | None:
    return REGISTRY.get(subject_id)


def all_subjects() -> list[Subject]:
    return list(REGISTRY.values())
