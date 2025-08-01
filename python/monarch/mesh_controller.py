# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import atexit
import logging
import os

import pdb  # noqa
import traceback
from collections import deque
from logging import Logger
from typing import (
    Any,
    cast,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
    Union,
)

import torch.utils._python_dispatch
from monarch._rust_bindings.monarch_extension import client
from monarch._rust_bindings.monarch_extension.client import (  # @manual=//monarch/monarch_extension:monarch_extension
    WorldState,
)
from monarch._rust_bindings.monarch_extension.mesh_controller import _Controller
from monarch._rust_bindings.monarch_hyperactor.actor import (
    PythonMessage,
    PythonMessageKind,
    UnflattenArg,
)
from monarch._rust_bindings.monarch_hyperactor.mailbox import Mailbox
from monarch._rust_bindings.monarch_hyperactor.proc import (  # @manual=//monarch/monarch_extension:monarch_extension
    ActorId,
)
from monarch._src.actor.actor_mesh import ActorEndpoint, Port, PortTuple
from monarch._src.actor.endpoint import Selection
from monarch._src.actor.shape import NDSlice
from monarch.common import device_mesh, messages, stream
from monarch.common.controller_api import TController
from monarch.common.invocation import Seq
from monarch.common.messages import Referenceable, SendResultOfActorCall
from monarch.common.stream import StreamRef
from monarch.common.tensor import InputChecker, Tensor
from monarch.tensor_worker_main import _set_trace

if TYPE_CHECKING:
    from monarch._rust_bindings.monarch_hyperactor.proc_mesh import (
        ProcMesh as HyProcMesh,
    )
    from monarch.actor import ProcMesh

from monarch._rust_bindings.monarch_hyperactor.shape import Point

from monarch.common.client import Client
from monarch.common.controller_api import LogMessage, MessageResult
from monarch.common.device_mesh import DeviceMesh
from monarch.common.future import Future as OldFuture
from monarch.common.invocation import DeviceException, RemoteException
from monarch.rust_local_mesh import _get_worker_exec_info

logger: Logger = logging.getLogger(__name__)


class Controller(_Controller):
    def __init__(self, workers: "HyProcMesh") -> None:
        super().__init__()
        self._mailbox: Mailbox = workers.client
        # Buffer for messages unrelated to debugging that are received while a
        # debugger session is active.
        self._non_debugger_pending_messages: deque[
            Optional[client.LogMessage | client.WorkerResponse]
        ] = deque()
        self._pending_debugger_sessions: deque[ActorId] = deque()

    def next_message(
        self, timeout: Optional[float]
    ) -> Optional[LogMessage | MessageResult]:
        raise RuntimeError(
            "internal error: tensor engine does not produce futures that call next_message"
        )

    def send(
        self,
        ranks: Union[NDSlice, List[NDSlice]],
        msg: NamedTuple,
    ) -> None:
        with torch.utils._python_dispatch._disable_current_modes():
            return super().send(ranks, msg)

    def drain_and_stop(
        self,
    ) -> List[LogMessage | MessageResult | client.DebuggerMessage]:
        self._drain_and_stop()
        return []

    def worker_world_state(self) -> WorldState:
        raise NotImplementedError("worker world state")

    def stop_mesh(self):
        # I think this is a noop?

        pass


def _initialize_env(worker_point: Point, proc_id: str) -> None:
    worker_rank = worker_point.rank
    try:
        _, worker_env = _get_worker_exec_info()
        local_rank = worker_point["gpus"]
        gpus_per_host = worker_point.size("gpus")
        num_worker_procs = len(worker_point.shape)
        process_env = {
            **worker_env,
            "CUDA_VISIBLE_DEVICES": str(local_rank),
            "NCCL_HOSTID": f"{proc_id}_host_{worker_rank // gpus_per_host}",
            # This is needed to avoid a hard failure in ncclx when we do not
            # have backend topology info (eg. on RE).
            "NCCL_IGNORE_TOPO_LOAD_FAILURE": "true",
            "LOCAL_RANK": str(local_rank),
            "RANK": str(worker_rank),
            "WORLD_SIZE": str(num_worker_procs),
            "LOCAL_WORLD_SIZE": str(gpus_per_host),
        }
        os.environ.update(process_env)
        pdb.set_trace = _set_trace
        # workaround for set_manual_seed somehow not working if cuda is not initialized\
        if torch.cuda.is_available():
            torch.cuda.init()
    except Exception:
        traceback.print_exc()
        raise


class MeshClient(Client):
    def fetch(
        self,
        mesh: "DeviceMesh",
        stream: "StreamRef",
        shard,
        preprocess_message,
        args,
        kwargs,
        defs: Tuple["Tensor", ...],
        uses: Tuple["Tensor", ...],
    ) -> "OldFuture":  # the OldFuture is a lie
        sender, receiver = PortTuple.create(self._mesh_controller._mailbox, once=True)

        ident = self.new_node(defs, uses, cast("OldFuture", sender))
        process = mesh._process(shard)
        self.send(
            process,
            messages.SendValue(
                ident,
                None,
                defs,
                preprocess_message,
                args,
                kwargs,
                stream,
            ),
        )
        # we have to ask for status updates
        # from workers to be sure they have finished
        # enough work to count this future as finished,
        # and all potential errors have been reported
        self._request_status()
        return cast("OldFuture", receiver.recv())

    def shutdown(
        self,
        destroy_pg: bool = True,
        error_reason: Optional[RemoteException | DeviceException | Exception] = None,
    ):
        # return
        if self.has_shutdown:
            return
        logger.info("shutting down the client gracefully")

        atexit.unregister(self._atexit)
        self._shutdown = True

        sender, receiver = PortTuple.create(self._mesh_controller._mailbox, once=True)
        assert sender._port_ref is not None
        self._mesh_controller.sync_at_exit(sender._port_ref.port_id)
        receiver.recv().get(timeout=60)
        # we are not expecting anything more now, because we already
        # waited for the responses
        self.inner.drain_and_stop()

    @property
    def _mesh_controller(self) -> Controller:
        return cast(Controller, self.inner)

    def new_node_nocoalesce(
        self,
        defs: Sequence["Tensor"],
        uses: Sequence["Tensor"],
        future: Optional["OldFuture"],
        tracebacks: List[List[traceback.FrameSummary]],
    ) -> Seq:
        seq = self._next_seq()
        for d in defs:
            d._seq = seq
        response_port = None
        if future is not None:
            # method annotation is a lie to make Client happy
            port, slice = cast("Tuple[Port[Any], NDSlice]", future)
            assert port._port_ref is not None
            response_port = (port._port_ref.port_id, slice)
        self._mesh_controller.node(seq, defs, uses, response_port, tracebacks)
        return seq

    def handle_next_message(self, timeout: Optional[float]) -> bool:
        """
        Mesh controller message loop is handled by the tokio event loop.
        """
        return False


def spawn_tensor_engine(proc_mesh: "ProcMesh") -> DeviceMesh:
    # This argument to Controller
    # is currently only used for debug printing. It should be fixed to
    # report the proc ID instead of the rank it currently does.
    gpus = proc_mesh.sizes.get("gpus", 1)
    backend_ctrl = Controller(proc_mesh._proc_mesh)
    client = MeshClient(cast("TController", backend_ctrl), proc_mesh.size(), gpus)
    dm = DeviceMesh(
        client,
        NDSlice.new_row_major(list(proc_mesh.sizes.values())),
        tuple(proc_mesh.sizes.keys()),
    )
    dm.exit = lambda: client.shutdown()
    return dm


class RemoteException(Exception):
    def __init__(
        self,
        worker_error_string: str,  # this should really be an exception + stacktrace but
        # worker code needs major refactor to make this possible
        controller_frames: List[traceback.FrameSummary],
        rank: int,
    ):
        self.worker_error_string = worker_error_string
        self.controller_frames = controller_frames
        self.rank = rank

    def __str__(self):
        try:
            controller_tb = "".join(traceback.format_list(self.controller_frames))
            return (
                f"A remote function has failed asynchronously on rank {self.rank}.\n"
                f"Traceback of where the remote function was issued on controller (most recent call last):\n{controller_tb}"
                f"Error as reported from worker:\n{self.worker_error_string}"
            )
        except Exception:
            traceback.print_exc()
            return "<exception formatting RemoteException>"


def actor_send(
    endpoint: ActorEndpoint,
    args_kwargs_tuple: bytes,
    refs: Sequence[Any],
    port: Optional[Port[Any]],
    selection: Selection,
):
    unflatten_args = [
        UnflattenArg.PyObject if isinstance(ref, Tensor) else UnflattenArg.Mailbox
        for ref in refs
    ]
    tensors = [ref for ref in refs if isinstance(ref, Tensor)]
    # we have some monarch references, we need to ensure their
    # proc_mesh matches that of the tensors we sent to it
    chosen_stream = stream._active
    for t in tensors:
        if hasattr(t, "stream"):
            chosen_stream = t.stream
            break
    with InputChecker(refs, lambda x: f"actor_call({x})") as checker:
        checker.check_mesh_stream_local(device_mesh._active, chosen_stream)
        # TODO: move propagators into Endpoint abstraction and run the propagator to get the
        # mutates
        checker.check_permission(())
    selected_device_mesh = (
        endpoint._actor_mesh._proc_mesh and endpoint._actor_mesh._proc_mesh._device_mesh
    )
    if selected_device_mesh is not checker.mesh:
        raise ValueError(
            f"monarch Tensors sent to an actor must be located on the same process as the actor. However {checker.mesh} is not {selected_device_mesh}."
            "NYI: better serialization of mesh names to make the mismatch more clear."
        )

    client = cast(MeshClient, checker.mesh.client)

    broker_id: Tuple[str, int] = client._mesh_controller.broker_id

    stream_ref = chosen_stream._to_ref(client)

    fut = (port, checker.mesh._ndslice) if port is not None else None

    ident = client.new_node([], tensors, cast("OldFuture", fut))

    # To ensure that both the actor and the stream execute in order, we send a message
    # to each at this point. The message to the worker will be handled on the stream actor where
    # it will send the 'tensor's to the broker actor locally, along with a response port with the
    # computed value.

    # The message to the generic actor tells it to first wait on the broker to get the local arguments
    # from the stream, then it will run the actor method, and send the result to response port.

    actor_msg = PythonMessage(
        PythonMessageKind.CallMethodIndirect(
            endpoint._name, broker_id, ident, unflatten_args
        ),
        args_kwargs_tuple,
    )
    endpoint._actor_mesh.cast(actor_msg, selection)
    worker_msg = SendResultOfActorCall(ident, broker_id, tensors, [], stream_ref)
    client.send(checker.mesh._ndslice, worker_msg)
    # we have to ask for status updates
    # from workers to be sure they have finished
    # enough work to count this future as finished,
    # and all potential errors have been reported
    client._request_status()
