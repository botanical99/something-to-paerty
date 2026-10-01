"""Transport interface the core/ layer will program against (Phase 3).

Status: INTERFACE ONLY. No implementation has been verified on real hardware yet.
Concrete adapters (Tuya gateway LAN, direct BLE mesh, simulator) will implement this so
core/ and the effects engine never depend on how bytes reach the lights.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LightCaps:
    dimmable: bool = True
    cct: bool = True
    rgb: bool = False
    brightness_range: tuple[int, int] = (10, 1000)   # raw transport units (to be measured)
    cct_range: tuple[int, int] = (0, 1000)


class LightTransport(ABC):
    """One adapter instance == one path to the physical lights."""

    is_simulated: bool = True  # adapters that really reach hardware must set False

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def list_lights(self) -> list[str]: ...

    @abstractmethod
    async def set_state(self, light_id: str, *, on: bool | None = None,
                        brightness_pct: float | None = None, cct_pct: float | None = None) -> None: ...
