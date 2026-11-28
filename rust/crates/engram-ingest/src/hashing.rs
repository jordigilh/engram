//! SHA-256 content hashing for the ingestion ledger.

use std::fmt;

use sha2::{Digest, Sha256};

/// A raw 32-byte SHA-256 digest. Stored as `bytea` in Postgres.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ContentHash([u8; 32]);

impl ContentHash {
    pub fn of(bytes: &[u8]) -> Self {
        Self(Sha256::digest(bytes).into())
    }

    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Self, HashError> {
        let array: [u8; 32] = bytes.try_into().map_err(|_| HashError::WrongLength {
            actual: bytes.len(),
        })?;
        Ok(Self(array))
    }

    pub fn from_hex(value: &str) -> Result<Self, HashError> {
        if value.len() != 64 {
            return Err(HashError::WrongLength {
                actual: value.len(),
            });
        }
        let mut bytes = [0u8; 32];
        for (index, byte) in bytes.iter_mut().enumerate() {
            let high = hex_nibble(value.as_bytes()[index * 2]).ok_or(HashError::NotHex)?;
            let low = hex_nibble(value.as_bytes()[index * 2 + 1]).ok_or(HashError::NotHex)?;
            *byte = (high << 4) | low;
        }
        Ok(Self(bytes))
    }

    pub fn to_hex(self) -> String {
        format!("{}", self)
    }
}

impl fmt::Display for ContentHash {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        for byte in self.0 {
            write!(formatter, "{byte:02x}")?;
        }
        Ok(())
    }
}

impl fmt::Debug for ContentHash {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "ContentHash({self})")
    }
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum HashError {
    #[error("expected a 32-byte SHA-256 value, got {actual} bytes")]
    WrongLength { actual: usize },
    #[error("value is not hexadecimal")]
    NotHex,
}

/// Hex SHA-256, matching `hashlib.sha256(...).hexdigest()`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    ContentHash::of(bytes).to_hex()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vector() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn hex_round_trip() {
        let hash = ContentHash::of(b"engram");
        assert_eq!(ContentHash::from_hex(&hash.to_hex()).unwrap(), hash);
        assert_eq!(
            ContentHash::from_hex("zz").unwrap_err(),
            HashError::WrongLength { actual: 2 }
        );
    }

    #[test]
    fn byte_length_is_enforced() {
        assert_eq!(
            ContentHash::from_bytes(&[0u8; 31]).unwrap_err(),
            HashError::WrongLength { actual: 31 }
        );
        assert!(ContentHash::from_bytes(&[0u8; 32]).is_ok());
    }

    #[test]
    fn hex_parsing_accepts_uppercase_and_rejects_non_hex() {
        let hash = ContentHash::of(b"engram");
        assert_eq!(
            ContentHash::from_hex(&hash.to_hex().to_uppercase()).unwrap(),
            hash
        );
        assert_eq!(
            ContentHash::from_hex(&"g".repeat(64)).unwrap_err(),
            HashError::NotHex
        );
    }
}
