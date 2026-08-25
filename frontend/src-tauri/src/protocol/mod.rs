mod codec;
mod models;

pub use codec::{encode_frame, FrameDecoder, ProtocolError};
pub use models::{
    FrameEnvelope, MessageType, RedactedFrameDebug, Sensitivity, MAX_HEADER_BYTES,
    MAX_PAYLOAD_BYTES, PROTOCOL_VERSION,
};
