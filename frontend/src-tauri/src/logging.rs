//! Fixed persistent logging policy owned by the Tauri Core process.

use tauri::{plugin::TauriPlugin, Runtime};
use tauri_plugin_log::{RotationStrategy, Target, TargetKind, TimezoneStrategy};

pub const MAX_LOG_FILE_SIZE_BYTES: u128 = 10 * 1024 * 1024;
pub const ARCHIVED_LOG_FILE_COUNT: usize = 4;
pub const LOG_FILE_NAME: &str = "harness-shell";

/// Build the mandatory terminal and application-log-directory targets.
pub fn plugin<R: Runtime>() -> TauriPlugin<R> {
    tauri_plugin_log::Builder::new()
        .level(log::LevelFilter::Info)
        .clear_targets()
        .targets([
            Target::new(TargetKind::Stdout),
            Target::new(TargetKind::LogDir {
                file_name: Some(LOG_FILE_NAME.to_owned()),
            }),
        ])
        .max_file_size(MAX_LOG_FILE_SIZE_BYTES)
        // KeepSome counts archives; the active file is additional.
        .rotation_strategy(RotationStrategy::KeepSome(ARCHIVED_LOG_FILE_COUNT))
        .timezone_strategy(TimezoneStrategy::UseUtc)
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persistent_log_policy_keeps_four_archives_plus_the_active_file() {
        assert_eq!(MAX_LOG_FILE_SIZE_BYTES, 10 * 1024 * 1024);
        assert_eq!(ARCHIVED_LOG_FILE_COUNT, 4);
        assert_eq!(LOG_FILE_NAME, "harness-shell");
    }
}
