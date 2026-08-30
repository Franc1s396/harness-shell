use serde_json::{Map, Value};

use super::models::{ManualSftpProgressEvent, MutationProgressProjection};

pub(crate) fn parse_manual_sftp_progress(
    payload: &Map<String, Value>,
) -> Result<MutationProgressProjection, serde_json::Error> {
    serde_json::from_value::<ManualSftpProgressEvent>(Value::Object(payload.clone()))
        .map(Into::into)
}
