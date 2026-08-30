use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    os::windows::{
        ffi::OsStrExt,
        fs::{MetadataExt, OpenOptionsExt},
        io::{AsRawHandle, FromRawHandle},
    },
    path::{Path, PathBuf},
    process::Command,
    ptr,
    sync::{Condvar, Mutex},
};

use sha2::{Digest, Sha256};
use uuid::Uuid;
use windows_sys::Win32::{
    Foundation::{
        CloseHandle, GetLastError, GENERIC_READ, GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE,
    },
    Storage::FileSystem::{
        CommitTransaction, CreateFileTransactedW, CreateTransaction, GetFileInformationByHandle,
        GetFileType, MoveFileExW, MoveFileTransactedW, RollbackTransaction,
        BY_HANDLE_FILE_INFORMATION, DELETE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        FILE_TYPE_DISK, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, OPEN_EXISTING,
    },
};

use super::models::require_js_safe;

/// Read-only upload source whose handle denies later write/delete sharing.
pub struct FrozenUploadSource {
    pub(crate) path: PathBuf,
    pub(crate) file: File,
    pub(crate) display_name: String,
    pub(crate) byte_count: u64,
    pub(crate) sha256: String,
}

impl FrozenUploadSource {
    pub fn display_name(&self) -> &str {
        &self.display_name
    }

    pub fn byte_count(&self) -> u64 {
        self.byte_count
    }

    pub fn sha256(&self) -> &str {
        &self.sha256
    }

    pub fn read_chunk(&mut self, maximum: usize) -> Result<Vec<u8>, LocalFileError> {
        if maximum > super::models::SFTP_CHUNK_BYTES {
            return Err(LocalFileError::new(
                "SFTP_CHUNK_TOO_LARGE",
                "The local transfer chunk exceeds the supported size.",
            ));
        }
        let mut chunk = vec![0_u8; maximum];
        let read = self
            .file
            .read(&mut chunk)
            .map_err(|_| local_read_failed())?;
        chunk.truncate(read);
        Ok(chunk)
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LocalFileIdentity {
    volume_serial: u32,
    file_index: u64,
    size: u64,
    creation_time: u64,
    last_write_time: u64,
    attributes: u32,
    // This remains Rust-private with the canonical target path and catches replacement that
    // preserves the file object, length, and visible timestamp fields.
    sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LocalTargetSnapshot {
    Absent,
    Existing(LocalFileIdentity),
}

/// Canonical local destination and immutable pre-execution snapshot.
///
/// This privileged type deliberately does not implement `Serialize`.
pub struct PreparedDownloadTarget {
    pub(crate) path: PathBuf,
    pub(crate) snapshot: LocalTargetSnapshot,
    pub(crate) display_name: String,
}

impl PreparedDownloadTarget {
    pub fn display_name(&self) -> &str {
        &self.display_name
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }
}

/// Same-directory download part retained on verification/commit failure for recovery.
pub struct LocalPartFile {
    file: Option<File>,
    part_path: PathBuf,
    target: PreparedDownloadTarget,
}

/// Safe result of inspecting a retained download part after a desktop restart.
///
/// The absolute path and content hash remain Rust-private and are never serializable.
pub struct LocalDownloadPartInspection {
    display_name: String,
    byte_count: u64,
}

impl LocalDownloadPartInspection {
    pub fn display_name(&self) -> &str {
        &self.display_name
    }

    pub fn byte_count(&self) -> u64 {
        self.byte_count
    }
}

#[cfg(debug_assertions)]
pub trait LocalFileSynchronizer {
    fn sync_all(&self, file: &File) -> io::Result<()>;
}

/// Test-only coordination for a real TxF move staged before its commit point.
///
/// Production never constructs this gate; it lets the contract test prove that a writer cannot
/// silently win between transactional move staging and `CommitTransaction`.
#[doc(hidden)]
pub struct TransactionCommitTestGate {
    state: Mutex<TransactionCommitTestState>,
    changed: Condvar,
}

#[doc(hidden)]
struct TransactionCommitTestState {
    block_next_commit: bool,
    block_before_move: bool,
    fail_next_move: bool,
    validation_complete: bool,
    move_released: bool,
    move_staged: bool,
    released: bool,
}

impl TransactionCommitTestGate {
    #[doc(hidden)]
    pub fn new() -> Self {
        Self {
            state: Mutex::new(TransactionCommitTestState {
                block_next_commit: false,
                block_before_move: false,
                fail_next_move: false,
                validation_complete: false,
                move_released: false,
                move_staged: false,
                released: false,
            }),
            changed: Condvar::new(),
        }
    }

    #[doc(hidden)]
    pub fn block_next_commit(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        state.block_next_commit = true;
        state.move_staged = false;
        state.released = false;
    }

    #[doc(hidden)]
    pub fn fail_next_move(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        state.fail_next_move = true;
    }

    #[doc(hidden)]
    pub fn block_before_move(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        state.block_before_move = true;
        state.validation_complete = false;
        state.move_released = false;
    }

    #[doc(hidden)]
    pub fn wait_until_validation_is_complete(&self) {
        let state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        let _state = self
            .changed
            .wait_while(state, |value| !value.validation_complete)
            .expect("transaction test gate mutex poisoned");
    }

    #[doc(hidden)]
    pub fn release_move(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        state.move_released = true;
        self.changed.notify_all();
    }

    #[doc(hidden)]
    pub fn wait_until_move_is_staged(&self) {
        let state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        let _state = self
            .changed
            .wait_while(state, |value| !value.move_staged)
            .expect("transaction test gate mutex poisoned");
    }

    #[doc(hidden)]
    pub fn release_commit(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        state.released = true;
        self.changed.notify_all();
    }

    fn after_move(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        if !state.block_next_commit {
            return;
        }
        state.move_staged = true;
        self.changed.notify_all();
        while !state.released {
            state = self
                .changed
                .wait(state)
                .expect("transaction test gate mutex poisoned");
        }
    }

    fn after_validation(&self) {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        if !state.block_before_move {
            return;
        }
        state.validation_complete = true;
        self.changed.notify_all();
        while !state.move_released {
            state = self
                .changed
                .wait(state)
                .expect("transaction test gate mutex poisoned");
        }
    }

    fn consume_move_failure(&self) -> bool {
        let mut state = self
            .state
            .lock()
            .expect("transaction test gate mutex poisoned");
        std::mem::take(&mut state.fail_next_move)
    }
}

impl LocalPartFile {
    pub fn create(
        target: PreparedDownloadTarget,
        operation_id: Uuid,
    ) -> Result<Self, LocalFileError> {
        let parent = target.path.parent().ok_or_else(invalid_local_path)?;
        let part_path = parent.join(format!(".harness-shell-download-{operation_id}.part"));
        if part_path.parent() != target.path.parent() {
            return Err(atomic_replace_unsupported());
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .share_mode(FILE_SHARE_READ)
            .open(&part_path)
            .map_err(|_| {
                LocalFileError::new(
                    "SFTP_LOCAL_PART_CREATE_FAILED",
                    "The local download part could not be created.",
                )
            })?;
        Ok(Self {
            file: Some(file),
            part_path,
            target,
        })
    }

    pub fn part_path(&self) -> &Path {
        &self.part_path
    }

    pub fn finish(self, expected_sha256: &str) -> Result<(), LocalFileError> {
        self.finish_with_sync_and_gate(expected_sha256, File::sync_all, None)
    }

    #[doc(hidden)]
    #[cfg(debug_assertions)]
    pub fn finish_with_synchronizer(
        self,
        expected_sha256: &str,
        synchronizer: &impl LocalFileSynchronizer,
    ) -> Result<(), LocalFileError> {
        self.finish_with_sync_and_gate(expected_sha256, |file| synchronizer.sync_all(file), None)
    }

    #[doc(hidden)]
    pub fn finish_with_transaction_commit_gate(
        self,
        expected_sha256: &str,
        gate: &TransactionCommitTestGate,
    ) -> Result<(), LocalFileError> {
        self.finish_with_sync_and_gate(expected_sha256, File::sync_all, Some(gate))
    }

    fn finish_with_sync_and_gate(
        mut self,
        expected_sha256: &str,
        synchronize: impl FnOnce(&File) -> io::Result<()>,
        transaction_gate: Option<&TransactionCommitTestGate>,
    ) -> Result<(), LocalFileError> {
        validate_sha256(expected_sha256)?;
        let mut file = self.file.take().ok_or_else(local_sync_failed)?;
        file.flush().map_err(|_| local_sync_failed())?;
        synchronize(&file).map_err(|_| local_sync_failed())?;
        drop(file);

        if matches!(self.target.snapshot, LocalTargetSnapshot::Existing(_)) {
            return commit_existing_target_txf(
                &self.part_path,
                &self.target,
                expected_sha256,
                transaction_gate,
            );
        }
        let actual_sha256 = hash_path(&self.part_path)?;
        if actual_sha256 != expected_sha256 {
            return Err(part_hash_mismatch());
        }
        revalidate_target(&self.target)?;
        commit_part(&self.part_path, &self.target)?;
        Ok(())
    }

    pub fn abort(mut self) -> Result<(), LocalFileError> {
        drop(self.file.take());
        fs::remove_file(&self.part_path).map_err(|_| {
            LocalFileError::new(
                "SFTP_LOCAL_CLEANUP_FAILED",
                "The local download part could not be removed.",
            )
        })
    }
}

/// Inspect a restart-retained download part without following a local reparse point.
///
/// The target path comes only from the DPAPI-protected journal. The part name is deterministically
/// derived from that target's parent and the journal operation identity, so the WebView can never
/// nominate an arbitrary local path for recovery.
pub fn inspect_download_part(
    target_path: &Path,
    operation_id: Uuid,
    expected_sha256: &str,
) -> Result<LocalDownloadPartInspection, LocalFileError> {
    validate_sha256(expected_sha256)?;
    let part_path = download_part_path(target_path, operation_id)?;
    let mut file = open_regular_read_locked(&part_path)?;
    let metadata = file.metadata().map_err(|_| local_file_unavailable())?;
    let byte_count = metadata.len();
    require_js_safe(byte_count).map_err(|_| {
        LocalFileError::new(
            "SFTP_FILE_SIZE_UNSUPPORTED",
            "The local file exceeds the supported desktop contract.",
        )
    })?;
    if hash_open_file(&mut file)? != expected_sha256 {
        return Err(part_hash_mismatch());
    }
    Ok(LocalDownloadPartInspection {
        display_name: display_name(target_path)?,
        byte_count,
    })
}

/// Open Explorer on a verified retained part using a fixed executable and fixed action.
///
/// No generic shell command or WebView-provided argument is accepted at this boundary.
pub fn open_download_part_folder(
    target_path: &Path,
    operation_id: Uuid,
    expected_sha256: &str,
) -> Result<LocalDownloadPartInspection, LocalFileError> {
    let inspection = inspect_download_part(target_path, operation_id, expected_sha256)?;
    let part_path = download_part_path(target_path, operation_id)?;
    let mut selection = std::ffi::OsString::from("/select,");
    selection.push(part_path.as_os_str());
    Command::new("explorer.exe")
        .arg(selection)
        .spawn()
        .map_err(|_| local_folder_open_failed())?;
    Ok(inspection)
}

impl Write for LocalPartFile {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.file
            .as_mut()
            .ok_or_else(|| io::Error::other("local part is closed"))?
            .write(buffer)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file
            .as_mut()
            .ok_or_else(|| io::Error::other("local part is closed"))?
            .flush()
    }
}

#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct LocalFileError {
    code: &'static str,
    message: &'static str,
}

impl LocalFileError {
    fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    pub fn code(&self) -> &'static str {
        self.code
    }
}

pub fn freeze_upload_source(path: &Path) -> Result<FrozenUploadSource, LocalFileError> {
    require_absolute_file_path(path)?;
    reject_reparse_point(path)?;
    let canonical = fs::canonicalize(path).map_err(|_| local_file_unavailable())?;
    let mut file = OpenOptions::new()
        .read(true)
        // Other readers are permitted; later writers and replacement/delete are denied.
        .share_mode(FILE_SHARE_READ)
        .open(&canonical)
        .map_err(|_| local_file_unavailable())?;
    let metadata = file.metadata().map_err(|_| local_file_unavailable())?;
    if !metadata.is_file() {
        return Err(local_file_unavailable());
    }
    let byte_count = metadata.len();
    require_js_safe(byte_count).map_err(|_| {
        LocalFileError::new(
            "SFTP_FILE_SIZE_UNSUPPORTED",
            "The local file exceeds the supported desktop contract.",
        )
    })?;
    let sha256 = hash_open_file(&mut file)?;
    file.seek(SeekFrom::Start(0))
        .map_err(|_| local_read_failed())?;
    let display_name = display_name(&canonical)?;
    Ok(FrozenUploadSource {
        path: canonical,
        file,
        display_name,
        byte_count,
        sha256,
    })
}

pub fn prepare_download_target(path: &Path) -> Result<PreparedDownloadTarget, LocalFileError> {
    require_absolute_file_path(path)?;
    let display_name = display_name(path)?;
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err(local_file_unavailable());
            }
            let canonical = fs::canonicalize(path).map_err(|_| local_file_unavailable())?;
            let identity = identity_for_path(&canonical)?;
            Ok(PreparedDownloadTarget {
                path: canonical,
                snapshot: LocalTargetSnapshot::Existing(identity),
                display_name,
            })
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let parent = path.parent().ok_or_else(invalid_local_path)?;
            let canonical_parent =
                fs::canonicalize(parent).map_err(|_| local_file_unavailable())?;
            let name = path.file_name().ok_or_else(invalid_local_path)?;
            Ok(PreparedDownloadTarget {
                path: canonical_parent.join(name),
                snapshot: LocalTargetSnapshot::Absent,
                display_name,
            })
        }
        Err(_) => Err(local_file_unavailable()),
    }
}

pub fn atomic_commit_local(
    target: PreparedDownloadTarget,
    bytes: &[u8],
) -> Result<(), LocalFileError> {
    let expected_sha256 = format!("{:x}", Sha256::digest(bytes));
    let mut part = LocalPartFile::create(target, Uuid::new_v4())?;
    part.write_all(bytes).map_err(|_| {
        LocalFileError::new(
            "SFTP_LOCAL_WRITE_FAILED",
            "The local download part could not be written.",
        )
    })?;
    part.finish(&expected_sha256)
}

fn hash_open_file(file: &mut File) -> Result<String, LocalFileError> {
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer).map_err(|_| local_read_failed())?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn hash_path(path: &Path) -> Result<String, LocalFileError> {
    let mut file = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(path)
        .map_err(|_| local_read_failed())?;
    hash_open_file(&mut file)
}

fn download_part_path(target_path: &Path, operation_id: Uuid) -> Result<PathBuf, LocalFileError> {
    require_absolute_file_path(target_path)?;
    let parent = target_path.parent().ok_or_else(invalid_local_path)?;
    let canonical_parent = fs::canonicalize(parent).map_err(|_| local_file_unavailable())?;
    if canonical_parent != parent {
        return Err(invalid_local_path());
    }
    let part_path = canonical_parent.join(format!(".harness-shell-download-{operation_id}.part"));
    if part_path.parent() != Some(canonical_parent.as_path()) {
        return Err(invalid_local_path());
    }
    Ok(part_path)
}

fn open_regular_read_locked(path: &Path) -> Result<File, LocalFileError> {
    let file = OpenOptions::new()
        .read(true)
        // Recovery inspection owns a stable identity while hashing. Writers and replacement are
        // denied until the hash result has been obtained.
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| local_file_unavailable())?;
    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0
        || information.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)
            != 0
        || unsafe { GetFileType(file.as_raw_handle()) } != FILE_TYPE_DISK
    {
        return Err(LocalFileError::new(
            "SFTP_LOCAL_REPARSE_POINT_UNSUPPORTED",
            "Local reparse-point or non-regular file selection is unsupported.",
        ));
    }
    Ok(file)
}

fn revalidate_target(target: &PreparedDownloadTarget) -> Result<(), LocalFileError> {
    match &target.snapshot {
        LocalTargetSnapshot::Absent => match fs::symlink_metadata(&target.path) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            _ => Err(target_changed()),
        },
        LocalTargetSnapshot::Existing(expected) => {
            let actual = identity_for_path(&target.path).map_err(|_| target_changed())?;
            if &actual == expected {
                Ok(())
            } else {
                Err(target_changed())
            }
        }
    }
}

fn identity_for_path(path: &Path) -> Result<LocalFileIdentity, LocalFileError> {
    reject_reparse_point(path)?;
    let mut file = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .open(path)
        .map_err(|_| local_file_unavailable())?;
    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: `information` is writable for the call and the owned file handle remains valid.
    let succeeded = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) };
    if succeeded == 0 {
        return Err(local_file_unavailable());
    }
    let sha256 = hash_open_file(&mut file)?;
    Ok(LocalFileIdentity {
        volume_serial: information.dwVolumeSerialNumber,
        file_index: ((information.nFileIndexHigh as u64) << 32) | information.nFileIndexLow as u64,
        size: ((information.nFileSizeHigh as u64) << 32) | information.nFileSizeLow as u64,
        creation_time: filetime_to_u64(information.ftCreationTime),
        last_write_time: filetime_to_u64(information.ftLastWriteTime),
        attributes: information.dwFileAttributes,
        sha256,
    })
}

fn filetime_to_u64(value: windows_sys::Win32::Foundation::FILETIME) -> u64 {
    ((value.dwHighDateTime as u64) << 32) | value.dwLowDateTime as u64
}

fn commit_part(part_path: &Path, target: &PreparedDownloadTarget) -> Result<(), LocalFileError> {
    let part_wide = wide_path(part_path)?;
    let target_wide = wide_path(&target.path)?;
    let succeeded = match target.snapshot {
        LocalTargetSnapshot::Absent => {
            // SAFETY: both UTF-16 buffers are NUL terminated and remain alive for the call.
            unsafe {
                MoveFileExW(
                    part_wide.as_ptr(),
                    target_wide.as_ptr(),
                    MOVEFILE_WRITE_THROUGH,
                )
            }
        }
        // Existing targets use the TxF-only path above. No ReplaceFileW fallback is permitted.
        LocalTargetSnapshot::Existing(_) => return Err(atomic_replace_unsupported()),
    };
    if succeeded == 0 {
        // Capture the Windows failure immediately; the stable public error remains path-free.
        let _windows_error = unsafe { GetLastError() };
        return Err(atomic_replace_unsupported());
    }
    Ok(())
}

fn commit_existing_target_txf(
    part_path: &Path,
    target: &PreparedDownloadTarget,
    expected_sha256: &str,
    transaction_gate: Option<&TransactionCommitTestGate>,
) -> Result<(), LocalFileError> {
    let expected_target = match &target.snapshot {
        LocalTargetSnapshot::Existing(identity) => identity,
        LocalTargetSnapshot::Absent => return Err(atomic_replace_unsupported()),
    };
    let transaction = TransactionHandle::create()?;
    let result = (|| {
        let mut target_file = open_transacted_regular(&target.path, transaction.raw())?;
        let observed_target = identity_for_open_file(&mut target_file)?;
        if &observed_target != expected_target {
            return Err(target_changed());
        }
        let mut part_file = open_transacted_regular(part_path, transaction.raw())?;
        if hash_open_file(&mut part_file)? != expected_sha256 {
            return Err(part_hash_mismatch());
        }
        // Both handles are no-follow transacted writers. TxF retains their writer locks for the
        // transaction after the handles close, while closing satisfies MoveFileTransactedW's
        // handle-lifetime rule. The move and CommitTransaction therefore remain inside the same
        // lock owner and no path-based writer can win after validation.
        drop(part_file);
        drop(target_file);
        if let Some(gate) = transaction_gate {
            gate.after_validation();
        }
        if transaction_gate.is_some_and(TransactionCommitTestGate::consume_move_failure) {
            return Err(atomic_replace_unsupported());
        }
        let part_wide = wide_path(part_path)?;
        let target_wide = wide_path(&target.path)?;
        let moved = unsafe {
            MoveFileTransactedW(
                part_wide.as_ptr(),
                target_wide.as_ptr(),
                None,
                ptr::null(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                transaction.raw(),
            )
        };
        if moved == 0 {
            let _windows_error = unsafe { GetLastError() };
            return Err(atomic_replace_unsupported());
        }
        if let Some(gate) = transaction_gate {
            gate.after_move();
        }
        transaction.commit()?;
        Ok(())
    })();
    if result.is_err() {
        transaction.rollback();
    }
    result
}

struct TransactionHandle {
    handle: HANDLE,
    committed: std::cell::Cell<bool>,
}

impl TransactionHandle {
    fn create() -> Result<Self, LocalFileError> {
        let handle =
            unsafe { CreateTransaction(ptr::null_mut(), ptr::null_mut(), 0, 0, 0, 0, ptr::null()) };
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            return Err(atomic_replace_unsupported());
        }
        Ok(Self {
            handle,
            committed: std::cell::Cell::new(false),
        })
    }

    fn raw(&self) -> HANDLE {
        self.handle
    }

    fn commit(&self) -> Result<(), LocalFileError> {
        if unsafe { CommitTransaction(self.handle) } == 0 {
            return Err(atomic_replace_unsupported());
        }
        self.committed.set(true);
        Ok(())
    }

    fn rollback(&self) {
        if !self.committed.get() {
            let _rolled_back = unsafe { RollbackTransaction(self.handle) };
        }
    }
}

impl Drop for TransactionHandle {
    fn drop(&mut self) {
        self.rollback();
        let _closed = unsafe { CloseHandle(self.handle) };
    }
}

fn open_transacted_regular(path: &Path, transaction: HANDLE) -> Result<File, LocalFileError> {
    let wide = wide_path(path)?;
    let handle = unsafe {
        CreateFileTransactedW(
            wide.as_ptr(),
            GENERIC_READ | GENERIC_WRITE | DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT,
            ptr::null_mut(),
            transaction,
            ptr::null(),
            ptr::null(),
        )
    };
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        return Err(atomic_replace_unsupported());
    }
    // SAFETY: CreateFileTransactedW returned a uniquely owned valid HANDLE.
    let file = unsafe { File::from_raw_handle(handle) };
    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0
        || information.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)
            != 0
        || unsafe { GetFileType(file.as_raw_handle()) } != FILE_TYPE_DISK
    {
        return Err(LocalFileError::new(
            "SFTP_LOCAL_REPARSE_POINT_UNSUPPORTED",
            "Local reparse-point or non-regular file selection is unsupported.",
        ));
    }
    Ok(file)
}

fn identity_for_open_file(file: &mut File) -> Result<LocalFileIdentity, LocalFileError> {
    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
        return Err(local_file_unavailable());
    }
    let sha256 = hash_open_file(file)?;
    Ok(LocalFileIdentity {
        volume_serial: information.dwVolumeSerialNumber,
        file_index: ((information.nFileIndexHigh as u64) << 32) | information.nFileIndexLow as u64,
        size: ((information.nFileSizeHigh as u64) << 32) | information.nFileSizeLow as u64,
        creation_time: filetime_to_u64(information.ftCreationTime),
        last_write_time: filetime_to_u64(information.ftLastWriteTime),
        attributes: information.dwFileAttributes,
        sha256,
    })
}

fn reject_reparse_point(path: &Path) -> Result<(), LocalFileError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| local_file_unavailable())?;
    if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(LocalFileError::new(
            "SFTP_LOCAL_REPARSE_POINT_UNSUPPORTED",
            "Local reparse-point file selection is unsupported.",
        ));
    }
    Ok(())
}

fn require_absolute_file_path(path: &Path) -> Result<(), LocalFileError> {
    if !path.is_absolute() || path.file_name().is_none() {
        return Err(invalid_local_path());
    }
    Ok(())
}

fn display_name(path: &Path) -> Result<String, LocalFileError> {
    let name = path.file_name().ok_or_else(invalid_local_path)?;
    let value = name.to_string_lossy().into_owned();
    if value.is_empty() {
        return Err(invalid_local_path());
    }
    Ok(value)
}

fn wide_path(path: &Path) -> Result<Vec<u16>, LocalFileError> {
    let mut value = path.as_os_str().encode_wide().collect::<Vec<_>>();
    if value.contains(&0) {
        return Err(invalid_local_path());
    }
    value.push(0);
    Ok(value)
}

fn validate_sha256(value: &str) -> Result<(), LocalFileError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(LocalFileError::new(
            "SFTP_HASH_INVALID",
            "The expected local file hash is invalid.",
        ));
    }
    Ok(())
}

fn invalid_local_path() -> LocalFileError {
    LocalFileError::new(
        "SFTP_LOCAL_PATH_INVALID",
        "The selected local path is invalid.",
    )
}

fn local_file_unavailable() -> LocalFileError {
    LocalFileError::new(
        "SFTP_LOCAL_FILE_UNAVAILABLE",
        "The selected local file is unavailable.",
    )
}

fn local_read_failed() -> LocalFileError {
    LocalFileError::new(
        "SFTP_LOCAL_READ_FAILED",
        "The local file could not be read.",
    )
}

fn local_sync_failed() -> LocalFileError {
    LocalFileError::new(
        "SFTP_LOCAL_SYNC_FAILED",
        "The local download part could not be synchronized.",
    )
}

fn part_hash_mismatch() -> LocalFileError {
    LocalFileError::new(
        "SFTP_HASH_MISMATCH",
        "The local download part did not match the expected hash.",
    )
}

fn target_changed() -> LocalFileError {
    LocalFileError::new(
        "SFTP_TARGET_CHANGED",
        "The local destination changed after confirmation.",
    )
}

fn atomic_replace_unsupported() -> LocalFileError {
    LocalFileError::new(
        "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
        "The local file system could not perform the required atomic replace.",
    )
}

fn local_folder_open_failed() -> LocalFileError {
    LocalFileError::new(
        "SFTP_LOCAL_FOLDER_OPEN_FAILED",
        "The local download part folder could not be opened.",
    )
}
