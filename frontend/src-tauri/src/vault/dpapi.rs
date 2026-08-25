use std::{ptr, slice};

use windows_sys::Win32::{
    Foundation::{GetLastError, LocalFree},
    Security::Cryptography::{
        CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    },
};
use zeroize::Zeroizing;

const MAX_BLOB_BYTES: usize = 1_048_576;

#[derive(Debug, thiserror::Error)]
pub enum DpapiError {
    #[error("{operation} rejected a blob with length {length}")]
    InvalidLength {
        operation: &'static str,
        length: usize,
    },
    #[error("{operation} failed with Windows error {code}")]
    Windows { operation: &'static str, code: u32 },
}

pub fn protect(plaintext: &[u8], description: &str) -> Result<Vec<u8>, DpapiError> {
    validate_length("CryptProtectData", plaintext.len())?;
    let plaintext = Zeroizing::new(plaintext.to_vec());
    let input = blob_for(plaintext.as_slice());
    let mut output = CRYPT_INTEGER_BLOB::default();
    let description: Vec<u16> = description.encode_utf16().chain(Some(0)).collect();

    // SAFETY: all input buffers remain alive for the call, optional pointers are null,
    // and the returned local-allocation is copied and freed below.
    let succeeded = unsafe {
        CryptProtectData(
            &input,
            description.as_ptr(),
            ptr::null(),
            ptr::null(),
            ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if succeeded == 0 {
        return Err(last_windows_error("CryptProtectData"));
    }

    // SAFETY: successful DPAPI calls return a DATA_BLOB owned by LocalFree.
    Ok(unsafe { copy_and_free(output) })
}

pub fn unprotect(ciphertext: &[u8]) -> Result<Zeroizing<Vec<u8>>, DpapiError> {
    validate_length("CryptUnprotectData", ciphertext.len())?;
    let input = blob_for(ciphertext);
    let mut output = CRYPT_INTEGER_BLOB::default();

    // SAFETY: all input buffers remain alive for the call, optional pointers are null,
    // and the returned local-allocation is copied and freed below.
    let succeeded = unsafe {
        CryptUnprotectData(
            &input,
            ptr::null_mut(),
            ptr::null(),
            ptr::null(),
            ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if succeeded == 0 {
        return Err(last_windows_error("CryptUnprotectData"));
    }

    // SAFETY: successful DPAPI calls return a DATA_BLOB owned by LocalFree.
    Ok(Zeroizing::new(unsafe { copy_and_free(output) }))
}

fn validate_length(operation: &'static str, length: usize) -> Result<(), DpapiError> {
    if length == 0 || length > MAX_BLOB_BYTES {
        return Err(DpapiError::InvalidLength { operation, length });
    }
    Ok(())
}

fn blob_for(bytes: &[u8]) -> CRYPT_INTEGER_BLOB {
    CRYPT_INTEGER_BLOB {
        cbData: bytes.len() as u32,
        pbData: bytes.as_ptr().cast_mut(),
    }
}

fn last_windows_error(operation: &'static str) -> DpapiError {
    // SAFETY: GetLastError has no preconditions and is called immediately after failure.
    let code = unsafe { GetLastError() };
    DpapiError::Windows { operation, code }
}

unsafe fn copy_and_free(blob: CRYPT_INTEGER_BLOB) -> Vec<u8> {
    let bytes = if blob.cbData == 0 {
        Vec::new()
    } else {
        // SAFETY: DPAPI returned `pbData` with exactly `cbData` initialized bytes.
        unsafe { slice::from_raw_parts(blob.pbData, blob.cbData as usize) }.to_vec()
    };
    // SAFETY: DPAPI allocates output with LocalAlloc; LocalFree accepts null as well.
    unsafe { LocalFree(blob.pbData.cast()) };
    bytes
}
