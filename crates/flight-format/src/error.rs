use std::fmt;

/// Errors produced while encoding or decoding `.flight` data.
///
/// Kept deliberately simple: writers surface these to the caller, readers
/// mostly *swallow* them and degrade to a `partial` file (rule 2 of the
/// format), so rich error taxonomy buys nothing here.
#[derive(Debug)]
pub enum FormatError {
    Io(std::io::Error),
    Encode(String),
    Decode(String),
    /// The file does not start with the `FLGT` magic.
    NotAFlightFile,
    /// The file declares a format version newer than this reader understands.
    UnsupportedVersion(u16),
}

impl fmt::Display for FormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FormatError::Io(e) => write!(f, "i/o error: {e}"),
            FormatError::Encode(e) => write!(f, "encode error: {e}"),
            FormatError::Decode(e) => write!(f, "decode error: {e}"),
            FormatError::NotAFlightFile => write!(f, "not a .flight file (bad magic)"),
            FormatError::UnsupportedVersion(v) => {
                write!(f, "unsupported .flight format version {v}")
            }
        }
    }
}

impl std::error::Error for FormatError {}

impl From<std::io::Error> for FormatError {
    fn from(e: std::io::Error) -> Self {
        FormatError::Io(e)
    }
}
