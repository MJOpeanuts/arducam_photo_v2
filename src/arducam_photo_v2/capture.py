from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .camera import GATE, CameraBusyError, CameraFaultError, acquire
from .config import Bank
from .processing import CaptureRenderError, archive_capture


@dataclass(frozen=True)
class Sequence:
    banks: tuple[Bank, ...]
    object_id: str | None = None
    object_description: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self):
        if not self.banks:
            raise ValueError("Sélectionnez au moins une banque.")
        if len({bank.id for bank in self.banks}) != len(self.banks):
            raise ValueError("Une banque ne peut apparaître qu’une fois dans la séquence.")
        for bank in self.banks:
            bank.validate()
            if bank.focus is None or bank.camera is None:
                raise ValueError(f"Banque incomplète : {bank.name}.")


def execute_sequence(sequence: Sequence, directory: Path, *,
                     progress: Callable[[str], None],
                     completed: Callable[[dict], None]) -> list[dict]:
    """Acquire once per frozen bank, stopping on uncertain camera ownership."""
    outputs = []
    stopped = False
    for position, bank in enumerate(sequence.banks, 1):
        output = {"bank": bank, "position": position, "sequence_id": sequence.id,
                  "capture_id": str(uuid4()), "status": "not_executed"}
        if not stopped:
            progress(f"Photo {position} sur {len(sequence.banks)} — Banque {bank.name}")
            acquisition = None
            try:
                acquisition = acquire(bank, output["capture_id"])
                stopped = bool(acquisition.release_error) or not acquisition.released
                try:
                    archive = archive_capture(
                        acquisition, bank, directory, sequence_id=sequence.id,
                        sequence_position=position, sequence_count=len(sequence.banks),
                        object_id=sequence.object_id, object_description=sequence.object_description)
                except CaptureRenderError as exc:
                    output.update(exc.archive, status="render_error", error=str(exc))
                else:
                    output.update(archive, status="complete")
                if acquisition.release_error or not acquisition.released:
                    output.update(status="release_error",
                                  error=" — ".join(filter(None, (
                                      output.get("error"),
                                      acquisition.release_error or "Libération caméra incertaine."))))
            except Exception as exc:
                output.update(status="failed", error=str(exc))
                stopped = stopped or isinstance(exc, (CameraFaultError, CameraBusyError))
            finally:
                acquisition = None
            stopped = stopped or bool(GATE.fault) or GATE.owner is not None
        else:
            output["error"] = "Non exécutée : état caméra incertain."
        outputs.append(output)
        completed(output.copy())
    return outputs
