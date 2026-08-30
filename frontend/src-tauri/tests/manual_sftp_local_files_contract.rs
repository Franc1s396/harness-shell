use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::windows::{
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawHandle,
    },
    process::Command,
    ptr,
    sync::Arc,
    thread,
};

use harness_shell_lib::sftp::local_files::{
    atomic_commit_local, freeze_upload_source, prepare_download_target, LocalFileSynchronizer,
    LocalPartFile, TransactionCommitTestGate,
};
use sha2::{Digest, Sha256};
use uuid::Uuid;
use windows_sys::Win32::{
    Foundation::FILETIME,
    Storage::FileSystem::{SetFileTime, FILE_SHARE_READ, FILE_SHARE_WRITE},
};

struct FailingSynchronizer;

impl LocalFileSynchronizer for FailingSynchronizer {
    fn sync_all(&self, _file: &File) -> io::Result<()> {
        Err(io::Error::other("injected sync failure"))
    }
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn frozen_upload_source_hashes_unicode_and_zero_byte_files() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("空文件.bin");
    fs::write(&path, []).unwrap();

    let mut frozen = freeze_upload_source(&path).unwrap();
    assert_eq!(frozen.display_name(), "空文件.bin");
    assert_eq!(frozen.byte_count(), 0);
    assert_eq!(frozen.sha256(), sha256(&[]));
    assert_eq!(frozen.read_chunk(16).unwrap(), Vec::<u8>::new());
}

#[test]
fn frozen_upload_source_denies_replacement_while_the_handle_is_owned() {
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("source.bin");
    let moved = directory.path().join("moved.bin");
    fs::write(&source, b"frozen").unwrap();

    let mut frozen = freeze_upload_source(&source).unwrap();
    assert!(OpenOptions::new().write(true).open(&source).is_err());
    assert!(fs::rename(&source, &moved).is_err());
    assert_eq!(frozen.byte_count(), 6);
    assert_eq!(frozen.read_chunk(16).unwrap(), b"frozen");
    assert_eq!(fs::read(&source).unwrap(), b"frozen");
}

#[test]
fn frozen_upload_source_rejects_a_local_reparse_point_without_following_it() {
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("destination");
    let reparse_point = directory.path().join("source-junction");
    fs::create_dir(&destination).unwrap();
    // A junction is a reparse point and, unlike a file symlink, needs no developer-mode
    // symlink privilege on this Windows test host.
    assert!(Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(&reparse_point)
        .arg(&destination)
        .status()
        .unwrap()
        .success());

    let error = match freeze_upload_source(&reparse_point) {
        Ok(_) => panic!("a local reparse point must not be followed"),
        Err(error) => error,
    };
    assert_eq!(error.code(), "SFTP_LOCAL_REPARSE_POINT_UNSUPPORTED");
}

#[test]
fn local_part_never_replaces_a_changed_target() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    fs::write(&target, b"changed").unwrap();

    let error = atomic_commit_local(prepared, b"download").unwrap_err();
    assert_eq!(error.code(), "SFTP_TARGET_CHANGED");
    assert_eq!(fs::read(&target).unwrap(), b"changed");
}

#[test]
fn local_part_rejects_changed_bytes_when_file_identity_size_and_mtime_are_restored() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let original_last_write_time = fs::metadata(&target).unwrap().last_write_time();
    let prepared = prepare_download_target(&target).unwrap();

    // This preserves the same file object and byte length while restoring the visible timestamp.
    // Only a Rust-private content hash can distinguish it from the confirmed destination.
    fs::write(&target, b"after!").unwrap();
    let file = OpenOptions::new()
        .write(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(&target)
        .unwrap();
    let last_write_time = FILETIME {
        dwLowDateTime: original_last_write_time as u32,
        dwHighDateTime: (original_last_write_time >> 32) as u32,
    };
    // SAFETY: the file handle is valid and the FILETIME value remains alive for the call.
    assert_ne!(
        unsafe {
            SetFileTime(
                file.as_raw_handle(),
                ptr::null(),
                ptr::null(),
                &last_write_time,
            )
        },
        0
    );
    drop(file);

    let error = atomic_commit_local(prepared, b"download").unwrap_err();
    assert_eq!(error.code(), "SFTP_TARGET_CHANGED");
    assert_eq!(fs::read(&target).unwrap(), b"after!");
}

#[test]
fn absent_target_appearing_before_commit_is_not_overwritten() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    part.write_all(b"download").unwrap();
    fs::write(&target, b"appeared").unwrap();

    let error = part.finish(&sha256(b"download")).unwrap_err();
    assert_eq!(error.code(), "SFTP_TARGET_CHANGED");
    assert_eq!(fs::read(&target).unwrap(), b"appeared");
}

#[test]
fn hash_mismatch_keeps_the_original_target_and_the_part_for_recovery() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    let part_path = part.part_path().to_path_buf();
    part.write_all(b"download").unwrap();

    let error = part.finish(&sha256(b"different")).unwrap_err();
    assert_eq!(error.code(), "SFTP_HASH_MISMATCH");
    assert_eq!(fs::read(&target).unwrap(), b"before");
    assert!(part_path.exists());
}

#[test]
fn successful_commit_is_same_directory_and_atomically_replaces_the_target() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    let canonical_parent = fs::canonicalize(target.parent().unwrap()).unwrap();
    assert_eq!(part.part_path().parent(), Some(canonical_parent.as_path()));
    part.write_all(b"download").unwrap();

    part.finish(&sha256(b"download")).unwrap();
    assert_eq!(fs::read(&target).unwrap(), b"download");
}

#[test]
fn existing_target_txf_commit_reserves_the_name_against_a_pre_commit_writer() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    let part_path = part.part_path().to_path_buf();
    part.write_all(b"download").unwrap();
    let gate = Arc::new(TransactionCommitTestGate::new());
    gate.block_next_commit();
    let worker_gate = Arc::clone(&gate);
    let worker = thread::spawn(move || {
        part.finish_with_transaction_commit_gate(&sha256(b"download"), &worker_gate)
    });

    gate.wait_until_move_is_staged();
    assert!(
        fs::write(&target, b"external writer").is_err(),
        "a pre-commit writer must be blocked or rejected, never silently replaced"
    );
    gate.release_commit();
    worker.join().unwrap().unwrap();
    assert_eq!(fs::read(&target).unwrap(), b"download");
    assert!(!part_path.exists());
}

#[test]
fn existing_target_txf_writer_lock_covers_validation_to_move_window() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    part.write_all(b"download").unwrap();
    let gate = Arc::new(TransactionCommitTestGate::new());
    gate.block_before_move();
    let worker_gate = Arc::clone(&gate);
    let worker = thread::spawn(move || {
        part.finish_with_transaction_commit_gate(&sha256(b"download"), &worker_gate)
    });

    gate.wait_until_validation_is_complete();
    let writer_was_rejected = fs::write(&target, b"external writer").is_err();
    gate.release_move();
    let finish_result = worker.join().unwrap();
    assert!(
        writer_was_rejected,
        "a transacted writer lock must survive handle close until the move is staged"
    );
    finish_result.unwrap();
    assert_eq!(fs::read(&target).unwrap(), b"download");
}

#[test]
fn existing_target_txf_error_keeps_target_and_part_without_replacefile_fallback() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    let part_path = part.part_path().to_path_buf();
    part.write_all(b"download").unwrap();
    let gate = TransactionCommitTestGate::new();
    gate.fail_next_move();

    let error = part
        .finish_with_transaction_commit_gate(&sha256(b"download"), &gate)
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_ATOMIC_REPLACE_UNSUPPORTED");
    assert_eq!(fs::read(&target).unwrap(), b"before");
    assert_eq!(fs::read(&part_path).unwrap(), b"download");
}

#[test]
fn sync_failure_never_commits_and_keeps_the_part_for_recovery() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    fs::write(&target, b"before").unwrap();
    let prepared = prepare_download_target(&target).unwrap();
    let mut part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    let part_path = part.part_path().to_path_buf();
    part.write_all(b"download").unwrap();

    let error = part
        .finish_with_synchronizer(&sha256(b"download"), &FailingSynchronizer)
        .unwrap_err();
    assert_eq!(error.code(), "SFTP_LOCAL_SYNC_FAILED");
    assert_eq!(fs::read(&target).unwrap(), b"before");
    assert!(part_path.exists());
}

#[test]
fn cleanup_failure_is_explicit_and_does_not_claim_the_part_was_removed() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.bin");
    let prepared = prepare_download_target(&target).unwrap();
    let part = LocalPartFile::create(prepared, Uuid::new_v4()).unwrap();
    let part_path = part.part_path().to_path_buf();
    let blocker = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(&part_path)
        .unwrap();

    let error = part.abort().unwrap_err();
    assert_eq!(error.code(), "SFTP_LOCAL_CLEANUP_FAILED");
    assert!(part_path.exists());
    drop(blocker);
    fs::remove_file(part_path).unwrap();
}
