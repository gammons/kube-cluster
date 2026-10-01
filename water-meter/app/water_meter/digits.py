import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from ai_edge_litert.interpreter import Interpreter

INPUT_WIDTH = 20
INPUT_HEIGHT = 32
CLASSES = 100


@dataclass(frozen=True)
class DigitResult:
    value: float
    confidence: float


class DigitReader:
    """Reads one odometer wheel with the AI-on-the-edge dig-class100 model (values 0.0-9.9)."""

    def __init__(self, model_path: Path):
        self._interpreter = Interpreter(model_path=str(model_path))
        self._interpreter.allocate_tensors()
        self._input = self._interpreter.get_input_details()[0]
        self._output = self._interpreter.get_output_details()[0]
        self._lock = threading.Lock()

    @property
    def input_shape(self) -> tuple[int, ...]:
        return tuple(int(v) for v in self._input["shape"])

    @property
    def output_shape(self) -> tuple[int, ...]:
        return tuple(int(v) for v in self._output["shape"])

    def read(self, crop: np.ndarray) -> DigitResult:
        resized = cv2.resize(crop, (INPUT_WIDTH, INPUT_HEIGHT), interpolation=cv2.INTER_LINEAR)
        tensor = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis]
        with self._lock:
            self._interpreter.set_tensor(self._input["index"], tensor)
            self._interpreter.invoke()
            logits = self._interpreter.get_tensor(self._output["index"])[0].astype(np.float64)

        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        best = int(np.argmax(probs))
        confidence = float(sum(probs[(best + k) % CLASSES] for k in (-1, 0, 1)))
        return DigitResult(value=best / 10.0, confidence=confidence)
