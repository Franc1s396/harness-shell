use std::{fs, path::PathBuf};

use harness_shell_lib::protocol::{
    encode_frame, FrameDecoder, FrameEnvelope, ProtocolError, Sensitivity, MAX_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
};
use serde_json::{json, Value};

fn fixture_bytes() -> Vec<u8> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../docs/protocol/fixtures/valid-heartbeat-v1.json");
    fs::read(path).expect("read protocol fixture")
}

fn heartbeat() -> FrameEnvelope {
    serde_json::from_slice(&fixture_bytes()).expect("deserialize heartbeat fixture")
}

#[test]
fn golden_fixture_round_trips_across_every_chunk_boundary() {
    let expected = heartbeat();
    let encoded = encode_frame(&expected).expect("encode frame");

    for split in 1..encoded.len() {
        let mut decoder = FrameDecoder::new();
        assert!(decoder.push(&encoded[..split]).unwrap().is_empty());
        assert_eq!(
            decoder.push(&encoded[split..]).unwrap(),
            vec![expected.clone()]
        );
    }
}

#[test]
fn decoder_returns_multiple_frames_from_one_chunk() {
    let first = heartbeat();
    let mut second = first.clone();
    second.sequence = 2;
    let mut wire = encode_frame(&first).unwrap();
    wire.extend(encode_frame(&second).unwrap());

    assert_eq!(
        FrameDecoder::new().push(&wire).unwrap(),
        vec![first, second]
    );
}

#[test]
fn decoder_rejects_oversized_payload_before_body_arrives() {
    let wire = format!("Content-Length: {}\r\n\r\n", MAX_PAYLOAD_BYTES + 1);

    assert!(matches!(
        FrameDecoder::new().push(wire.as_bytes()),
        Err(ProtocolError::FrameTooLarge { .. })
    ));
}

#[test]
fn decoder_rejects_the_python_malformed_header_corpus() {
    let cases: &[&[u8]] = &[
        b"Content-Length: 2\r\nX-Test: 1\r\n\r\n{}",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"content-length: 2\r\n\r\n{}",
        b"Content-Length: -1\r\n\r\n",
        b"Content-Length: abc\r\n\r\n",
    ];

    for wire in cases {
        assert!(matches!(
            FrameDecoder::new().push(wire),
            Err(ProtocolError::InvalidHeader)
        ));
    }
}

#[test]
fn decoder_rejects_header_over_limit_before_delimiter() {
    let mut wire = b"Content-Length: ".to_vec();
    wire.extend(vec![b'1'; MAX_HEADER_BYTES]);

    assert!(matches!(
        FrameDecoder::new().push(&wire),
        Err(ProtocolError::HeaderTooLarge { .. })
    ));
}

#[test]
fn decoder_rejects_the_python_invalid_payload_corpus() {
    let bodies: &[&[u8]] = &[b"\xff", b"{", b"[]", br#"{"protocol_version":1}"#];

    for body in bodies {
        let mut wire = format!("Content-Length: {}\r\n\r\n", body.len()).into_bytes();
        wire.extend(*body);
        assert!(matches!(
            FrameDecoder::new().push(&wire),
            Err(ProtocolError::InvalidEnvelope)
        ));
    }
}

#[test]
fn decoder_surfaces_unsupported_protocol_version_for_supervisor_mapping() {
    let mut body: Value = serde_json::from_slice(&fixture_bytes()).unwrap();
    body["protocol_version"] = json!(2);
    let body = serde_json::to_vec(&body).unwrap();
    let mut wire = format!("Content-Length: {}\r\n\r\n", body.len()).into_bytes();
    wire.extend(body);

    assert_eq!(
        FrameDecoder::new().push(&wire),
        Err(ProtocolError::UnsupportedProtocolVersion { actual: 2 })
    );
}

#[test]
fn envelope_rejects_invalid_fields_unknown_fields_and_non_object_payload() {
    let baseline: Value = serde_json::from_slice(&fixture_bytes()).unwrap();
    let mutations = [
        ("protocol_version", json!(2)),
        ("sequence", json!(0)),
        ("unexpected", json!(true)),
        ("payload", json!(["not", "an", "object"])),
    ];

    for (field, value) in mutations {
        let mut candidate = baseline.clone();
        candidate
            .as_object_mut()
            .unwrap()
            .insert(field.into(), value);
        assert!(serde_json::from_value::<FrameEnvelope>(candidate).is_err());
    }
}

#[test]
fn encoder_refuses_invalid_in_memory_envelopes() {
    let mut frame = heartbeat();
    frame.protocol_version = 2;
    assert_eq!(encode_frame(&frame), Err(ProtocolError::InvalidEnvelope));

    frame.protocol_version = 1;
    frame.sequence = 0;
    assert_eq!(encode_frame(&frame), Err(ProtocolError::InvalidEnvelope));
}

#[test]
fn decoder_clears_buffer_after_terminal_violation() {
    let mut decoder = FrameDecoder::new();
    assert!(decoder
        .push(b"Content-Length: 2\r\nX-Test: 1\r\n\r\n{}")
        .is_err());

    assert_eq!(
        decoder.push(&encode_frame(&heartbeat()).unwrap()).unwrap(),
        vec![heartbeat()]
    );
}

#[test]
fn redacted_debug_never_formats_payload_values() {
    let mut frame = heartbeat();
    frame.sensitivity = Sensitivity::Secret;
    frame.payload = json!({"api_key": "M1-RUST-SECRET-MARKER"})
        .as_object()
        .unwrap()
        .clone();
    let secret_debug = format!("{:?}", frame.redacted_debug());
    assert!(!secret_debug.contains("M1-RUST-SECRET-MARKER"));
    assert!(secret_debug.contains("payload_bytes"));

    frame.sensitivity = Sensitivity::Normal;
    let normal_debug = format!("{:?}", frame.redacted_debug());
    assert!(normal_debug.contains("api_key"));
    assert!(!normal_debug.contains("M1-RUST-SECRET-MARKER"));
}
