# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import io
import pickle
from contextlib import contextmanager, ExitStack
from typing import Any, Callable, Iterable, List, Tuple

import cloudpickle

try:
    import torch  # @manual
except ImportError:
    torch = None


class _Pickler(cloudpickle.Pickler):
    def __init__(self, filter):
        self.f = io.BytesIO()
        super().__init__(self.f)
        self._filter = filter
        self._saved = []

    def persistent_id(self, obj):
        if not self._filter(obj):
            return None
        self._saved.append(obj)
        return len(self._saved) - 1


class _Unpickler(pickle.Unpickler):
    def __init__(self, data, sequence: Iterable[Any]):
        super().__init__(io.BytesIO(data))
        self._iter = iter(sequence)
        self._values = []

    def persistent_load(self, id):
        while id >= len(self._values):
            self._values.append(next(self._iter))
        return self._values[id]


def flatten(obj: Any, filter: Callable[[Any], bool]) -> Tuple[List[Any], bytes]:
    pickler = _Pickler(filter)
    pickler.dump(obj)
    return pickler._saved, pickler.f.getvalue()


def unflatten(data: bytes, values: Iterable[Any]) -> Any:
    with ExitStack() as stack:
        if torch is not None:
            stack.enter_context(load_tensors_on_cpu())
            stack.enter_context(torch.utils._python_dispatch._disable_current_modes())
        up = _Unpickler(data, values)
        return up.load()


@contextmanager
def load_tensors_on_cpu():
    # Ensure that any tensors load from CPU via monkeypatching how Storages are
    # loaded.
    old = torch.storage._load_from_bytes
    try:
        torch.storage._load_from_bytes = lambda b: torch.load(
            io.BytesIO(b), map_location="cpu", weights_only=False
        )
        yield
    finally:
        torch.storage._load_from_bytes = old
