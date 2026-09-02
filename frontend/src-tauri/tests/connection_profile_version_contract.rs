use harness_shell_lib::runtime::{ConnectSshRequest, RuntimeHttpRequest, RuntimeRequestBody};
use uuid::Uuid;

#[test]
fn ssh_authentication_uses_numeric_profile_versions() {
    let request = ConnectSshRequest::password(Uuid::new_v4(), 7, "cGFzc3dvcmQ=".to_owned(), None);
    let body = request.body().unwrap();
    let RuntimeRequestBody::Json(bytes) = &body else {
        panic!("SSH connect must use JSON")
    };
    let payload: serde_json::Value = serde_json::from_slice(bytes).unwrap();

    assert_eq!(payload["profile_version"], 7);
    assert!(payload.get("profile_updated_at").is_none());
}
