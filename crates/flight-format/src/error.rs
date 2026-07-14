use std::fmt;


#[derive(Debug)]
pub enum FormatError {
    Io(std::io::Error),
    Encode(String),
    Decode(String),

    NotAFlightFile,

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
