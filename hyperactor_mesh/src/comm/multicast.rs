/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

//! The comm actor that provides message casting and result accumulation.

use hyperactor::Actor;
use hyperactor::Context;
use hyperactor::Named;
use hyperactor::RemoteHandles;
use hyperactor::RemoteMessage;
use hyperactor::actor::RemoteActor;
use hyperactor::attrs::Attrs;
use hyperactor::data::Serialized;
use hyperactor::declare_attrs;
use hyperactor::message::Castable;
use hyperactor::message::ErasedUnbound;
use hyperactor::message::IndexedErasedUnbound;
use hyperactor::reference::ActorId;
use ndslice::Shape;
use ndslice::Slice;
use ndslice::selection::Selection;
use ndslice::selection::routing::RoutingFrame;
use serde::Deserialize;
use serde::Serialize;

use crate::reference::ActorMeshId;

/// A union of slices that can be used to represent arbitrary subset of
/// ranks in a gang. It is represented by a Slice together with a Selection.
/// This is used to define the destination of a cast message or the source of
/// accumulation request.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Uslice {
    /// A slice representing a whole gang.
    pub slice: Slice,
    /// A selection used to represent any subset of the gang.
    pub selection: Selection,
}

/// An envelope that carries a message destined to a group of actors.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq, Named)]
pub struct CastMessageEnvelope {
    /// The destination actor mesh id.
    actor_mesh_id: ActorMeshId,
    /// The sender of this message.
    sender: ActorId,
    /// The destination port of the message. It could match multiple actors with
    /// rank wildcard.
    dest_port: DestinationPort,
    /// The serialized message.
    data: ErasedUnbound,
    /// The shape of the cast.
    shape: Shape,
}

impl CastMessageEnvelope {
    /// Create a new CastMessageEnvelope.
    pub fn new<A, M>(
        actor_mesh_id: ActorMeshId,
        sender: ActorId,
        shape: Shape,
        message: M,
    ) -> Result<Self, anyhow::Error>
    where
        A: RemoteActor + RemoteHandles<IndexedErasedUnbound<M>>,
        M: Castable + RemoteMessage,
    {
        let data = ErasedUnbound::try_from_message(message)?;
        let actor_name = actor_mesh_id.1.to_string();
        Ok(Self {
            actor_mesh_id,
            sender,
            dest_port: DestinationPort::new::<A, M>(actor_name),
            data,
            shape,
        })
    }

    /// Create a new CastMessageEnvelope from serialized data. Only use this
    /// when the message do not contain reply ports. Or it does but you are okay
    /// with the destination actors reply to the client actor directly.
    pub fn from_serialized(
        actor_mesh_id: ActorMeshId,
        sender: ActorId,
        dest_port: DestinationPort,
        shape: Shape,
        data: Serialized,
    ) -> Self {
        Self {
            actor_mesh_id,
            sender,
            dest_port,
            data: ErasedUnbound::new(data),
            shape,
        }
    }

    pub(crate) fn sender(&self) -> &ActorId {
        &self.sender
    }

    pub(crate) fn dest_port(&self) -> &DestinationPort {
        &self.dest_port
    }

    pub(crate) fn data(&self) -> &ErasedUnbound {
        &self.data
    }

    pub(crate) fn data_mut(&mut self) -> &mut ErasedUnbound {
        &mut self.data
    }

    pub(crate) fn shape(&self) -> &Shape {
        &self.shape
    }

    /// The unique key used to indicate the stream to which to deliver this message.
    /// Concretely, the comm actors along the path should use this key to manage
    /// sequence numbers and reorder buffers.
    pub(crate) fn stream_key(&self) -> (ActorMeshId, ActorId) {
        (self.actor_mesh_id.clone(), self.sender.clone())
    }
}

/// Destination port id of a message. It is a `PortId` with the rank masked out,
/// and the messege is always sent to the root actor because only root actor
/// can be accessed externally. The rank is resolved by the destination Selection
/// of the message. We can use `DestinationPort::port_id(rank)` to get the actual
/// `PortId` of the message.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq, Named)]
pub struct DestinationPort {
    /// The actor name to which the message should be delivered.
    actor_name: String,
    /// The port index of the destination actors, it is derived from the
    /// message type and cached here.
    port: u64,
}

impl DestinationPort {
    /// Create a new DestinationPort for a global actor name and message type.
    pub fn new<A, M>(actor_name: String) -> Self
    where
        A: RemoteActor + RemoteHandles<IndexedErasedUnbound<M>>,
        M: Castable + RemoteMessage,
    {
        Self {
            actor_name,
            port: IndexedErasedUnbound::<M>::port(),
        }
    }

    /// The port id of the destination.
    pub fn port(&self) -> u64 {
        self.port
    }

    /// Get the actor name of the destination.
    pub fn actor_name(&self) -> &str {
        &self.actor_name
    }
}

/// The is used to start casting a message to a group of actors.
#[derive(Serialize, Deserialize, Debug, Clone, Named)]
pub struct CastMessage {
    /// The cast destination.
    pub dest: Uslice,
    /// The message to cast.
    pub message: CastMessageEnvelope,
}

/// Forward a message to procs of next hops. This is used by comm actor to
/// forward a message to other comm actors following the selection topology.
/// This message is not visible to the clients.
#[derive(Serialize, Deserialize, Debug, Clone, Named)]
pub(crate) struct ForwardMessage {
    /// The comm actor who originally casted the message.
    pub(crate) sender: ActorId,
    /// The destination of the message.
    pub(crate) dests: Vec<RoutingFrame>,
    /// The sequence number of this message.
    pub(crate) seq: usize,
    /// The sequence number of the previous message receieved.
    pub(crate) last_seq: usize,
    /// The message to distribute.
    pub(crate) message: CastMessageEnvelope,
}

declare_attrs! {
    /// Used inside headers for cast messages to store
    /// the rank of the receiver.
    attr CAST_RANK: usize;
    /// Used inside headers to store the shape of the
    /// actor mesh that a message was cast to.
    attr CAST_SHAPE: Shape;
    /// Used inside headers to store the originating sender of a cast.
    pub attr CAST_ORIGINATING_SENDER: ActorId;
}

pub fn set_cast_info_on_headers(headers: &mut Attrs, rank: usize, shape: Shape, sender: ActorId) {
    headers.set(CAST_RANK, rank);
    headers.set(CAST_SHAPE, shape);
    headers.set(CAST_ORIGINATING_SENDER, sender);
}

pub trait CastInfo {
    /// Get the cast rank and cast shape.
    /// If something wasn't explicitly sent via a cast, then
    /// we represent it as the only member of a 0-dimensonal cast shape,
    /// which is the same as a singleton.
    fn cast_info(&self) -> (usize, Shape);
}

impl<A: Actor> CastInfo for Context<'_, A> {
    fn cast_info(&self) -> (usize, Shape) {
        let headers = self.headers();
        match (headers.get(CAST_RANK), headers.get(CAST_SHAPE)) {
            (Some(rank), Some(shape)) => (*rank, shape.clone()),
            (None, None) => (0, Shape::unity()),
            _ => panic!("Expected either both rank and shape or neither"),
        }
    }
}
