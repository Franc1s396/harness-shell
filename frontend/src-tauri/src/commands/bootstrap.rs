use std::ffi::OsStr;

use serde::Serialize;
use tauri::State;
use url::Url;

use super::CommandError;

/// Immutable loopback address supplied by the independent desktop Launcher.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct BackendBootstrap {
    pub backend_base_url: String,
}

/// Safe startup parsing error. It deliberately carries no raw argument value.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BootstrapArgumentError;

impl std::fmt::Display for BootstrapArgumentError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BACKEND_BOOTSTRAP_INVALID")
    }
}

impl std::error::Error for BootstrapArgumentError {}

impl BackendBootstrap {
    /// Accept only the exact unauthenticated dynamic IPv4 loopback HTTP origin.
    pub fn parse(value: &str) -> Result<Self, BootstrapArgumentError> {
        let url = Url::parse(value).map_err(|_| BootstrapArgumentError)?;
        let port = url.port().ok_or(BootstrapArgumentError)?;
        if url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || port == 0
            || !url.username().is_empty()
            || url.password().is_some()
            || url.path() != "/"
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(BootstrapArgumentError);
        }
        Ok(Self {
            backend_base_url: format!("http://127.0.0.1:{port}"),
        })
    }

    /// Parse one optional `--backend-url <origin>` pair and reject all other UI arguments.
    pub fn from_args<I, S>(args: I) -> Result<Option<Self>, BootstrapArgumentError>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        let mut arguments = args.into_iter();
        let _program = arguments.next();
        let mut bootstrap = None;
        while let Some(argument) = arguments.next() {
            if argument.as_ref() != OsStr::new("--backend-url") || bootstrap.is_some() {
                return Err(BootstrapArgumentError);
            }
            let value = arguments.next().ok_or(BootstrapArgumentError)?;
            let value = value.as_ref().to_str().ok_or(BootstrapArgumentError)?;
            bootstrap = Some(Self::parse(value)?);
        }
        Ok(bootstrap)
    }
}

/// Tauri-managed immutable bootstrap state. Debug mode may intentionally hold no URL.
pub struct BackendBootstrapState(Option<BackendBootstrap>);

impl BackendBootstrapState {
    pub fn new(value: Option<BackendBootstrap>) -> Self {
        Self(value)
    }
}

#[tauri::command]
pub fn get_backend_bootstrap(
    state: State<'_, BackendBootstrapState>,
) -> Result<BackendBootstrap, CommandError> {
    state.0.clone().ok_or_else(|| {
        CommandError::new(
            "BACKEND_BOOTSTRAP_MISSING",
            "The Backend bootstrap address is unavailable.",
        )
    })
}
