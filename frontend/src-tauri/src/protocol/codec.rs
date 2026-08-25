use super::models::{FrameEnvelope, MAX_HEADER_BYTES, MAX_PAYLOAD_BYTES, PROTOCOL_VERSION};

const HEADER_DELIMITER: &[u8] = b"\r\n\r\n";
const CONTENT_LENGTH_PREFIX: &[u8] = b"Content-Length: ";

#[derive(Debug, Eq, PartialEq, thiserror::Error)]
pub enum ProtocolError {
    #[error("frame header exceeds {max} bytes")]
    HeaderTooLarge { max: usize },
    #[error("frame header must contain exactly one case-sensitive Content-Length")]
    InvalidHeader,
    #[error("frame payload length {actual} exceeds {max} bytes")]
    FrameTooLarge { actual: usize, max: usize },
    #[error("frame payload is not a valid v1 envelope")]
    InvalidEnvelope,
}

pub fn encode_frame(frame: &FrameEnvelope) -> Result<Vec<u8>, ProtocolError> {
    if frame.protocol_version != PROTOCOL_VERSION || frame.sequence == 0 {
        return Err(ProtocolError::InvalidEnvelope);
    }
    let body = serde_json::to_vec(frame).map_err(|_| ProtocolError::InvalidEnvelope)?;
    if body.len() > MAX_PAYLOAD_BYTES {
        return Err(ProtocolError::FrameTooLarge {
            actual: body.len(),
            max: MAX_PAYLOAD_BYTES,
        });
    }

    let mut encoded = format!("Content-Length: {}\r\n\r\n", body.len()).into_bytes();
    encoded.extend(body);
    Ok(encoded)
}

#[derive(Default)]
pub struct FrameDecoder {
    buffer: Vec<u8>,
}

impl FrameDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, chunk: &[u8]) -> Result<Vec<FrameEnvelope>, ProtocolError> {
        self.buffer.extend_from_slice(chunk);
        let result = self.decode_available();
        if result.is_err() {
            self.buffer.clear();
        }
        result
    }

    fn decode_available(&mut self) -> Result<Vec<FrameEnvelope>, ProtocolError> {
        let mut frames = Vec::new();
        loop {
            let Some(header_end) = find_subslice(&self.buffer, HEADER_DELIMITER) else {
                if self.buffer.len() > MAX_HEADER_BYTES {
                    return Err(ProtocolError::HeaderTooLarge {
                        max: MAX_HEADER_BYTES,
                    });
                }
                return Ok(frames);
            };

            if header_end > MAX_HEADER_BYTES {
                return Err(ProtocolError::HeaderTooLarge {
                    max: MAX_HEADER_BYTES,
                });
            }
            let content_length = parse_header(&self.buffer[..header_end])?;
            if content_length > MAX_PAYLOAD_BYTES {
                return Err(ProtocolError::FrameTooLarge {
                    actual: content_length,
                    max: MAX_PAYLOAD_BYTES,
                });
            }

            let body_start = header_end + HEADER_DELIMITER.len();
            let frame_end = body_start + content_length;
            if self.buffer.len() < frame_end {
                return Ok(frames);
            }

            let body = self.buffer[body_start..frame_end].to_vec();
            self.buffer.drain(..frame_end);
            let frame =
                serde_json::from_slice(&body).map_err(|_| ProtocolError::InvalidEnvelope)?;
            frames.push(frame);
        }
    }
}

fn parse_header(header: &[u8]) -> Result<usize, ProtocolError> {
    if header.contains(&b'\r')
        || header.contains(&b'\n')
        || !header.starts_with(CONTENT_LENGTH_PREFIX)
    {
        return Err(ProtocolError::InvalidHeader);
    }

    let value = &header[CONTENT_LENGTH_PREFIX.len()..];
    if value.is_empty() || !value.iter().all(u8::is_ascii_digit) {
        return Err(ProtocolError::InvalidHeader);
    }
    std::str::from_utf8(value)
        .ok()
        .and_then(|value| value.parse().ok())
        .ok_or(ProtocolError::InvalidHeader)
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}
