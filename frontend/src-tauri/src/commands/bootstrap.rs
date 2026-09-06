use std::ffi::OsStr;

use serde::Serialize;
use tauri::State;
use url::Url;

use super::CommandError;

/// 独立桌面 Launcher 提供的不可变 loopback 地址。
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct BackendBootstrap {
    pub backend_base_url: String,
}

/// 安全启动解析错误，刻意不携带原始参数值。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BootstrapArgumentError;

impl std::fmt::Display for BootstrapArgumentError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BACKEND_BOOTSTRAP_INVALID")
    }
}

impl std::error::Error for BootstrapArgumentError {}

impl BackendBootstrap {
    /// 只接受精确、无认证、动态端口的 IPv4 loopback HTTP origin。
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

    /// 解析可选的单个 `--backend-url <origin>` 参数对，拒绝所有其他 UI 参数。
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

/// Tauri 管理的不可变 bootstrap 状态；调试模式可有意不配置 URL。
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
