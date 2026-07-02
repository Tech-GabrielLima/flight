use serde::{Deserialize, Serialize};

/// What happened at one point of the execution.
///
/// The numeric values are part of the on-disk format — never renumber.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u8)]
pub enum EventKind {
    /// A Python function started (sys.monitoring PY_START).
    PyStart = 1,
    /// A Python function returned (PY_RETURN).
    PyReturn = 2,
    /// A new source line began executing (LINE).
    Line = 3,
    /// An exception was raised (RAISE).
    Raise = 4,
    /// An exception was re-raised (RERAISE).
    Reraise = 5,
    /// An exception unwound a frame without a return (PY_UNWIND).
    PyUnwind = 6,
}

impl EventKind {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            1 => Some(EventKind::PyStart),
            2 => Some(EventKind::PyReturn),
            3 => Some(EventKind::Line),
            4 => Some(EventKind::Raise),
            5 => Some(EventKind::Reraise),
            6 => Some(EventKind::PyUnwind),
            _ => None,
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            EventKind::PyStart => "PY_START",
            EventKind::PyReturn => "PY_RETURN",
            EventKind::Line => "LINE",
            EventKind::Raise => "RAISE",
            EventKind::Reraise => "RERAISE",
            EventKind::PyUnwind => "PY_UNWIND",
        }
    }
}

/// One execution event, as stored in the ring buffer and in EVENT_RING blocks.
///
/// Fixed 24-byte layout so a ring slot fits comfortably in a cache line.
/// Serialized with msgpack as a positional array: `[kind, thread, line,
/// code_id, tstamp]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(C)]
pub struct Event {
    /// Discriminant, see [`EventKind`]. Stored raw so pushes stay branch-free.
    pub kind: u8,
    /// Flight-assigned compact thread id (not the OS tid).
    pub thread: u16,
    /// Source line for LINE events; 0 otherwise.
    pub line: u32,
    /// Identity of the code object (`id(code)` on the Python side). Resolved
    /// to file/qualname through [`CodeInfo`] entries in the same block.
    pub code_id: u64,
    /// Global *logical* timestamp: a monotonically increasing counter, not
    /// wall time. Orders events across threads with no clock cost.
    pub tstamp: u64,
}

impl Event {
    pub fn new(kind: EventKind, thread: u16, line: u32, code_id: u64, tstamp: u64) -> Self {
        Event {
            kind: kind as u8,
            thread,
            line,
            code_id,
            tstamp,
        }
    }

    pub fn kind(&self) -> Option<EventKind> {
        EventKind::from_u8(self.kind)
    }
}

/// Sidecar metadata resolving a `code_id` to something a human can read.
///
/// Captured once, on the first PY_START of each code object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeInfo {
    pub file: String,
    pub qualname: String,
    pub first_line: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_is_24_bytes() {
        assert_eq!(std::mem::size_of::<Event>(), 24);
    }

    #[test]
    fn kind_roundtrip() {
        for k in [
            EventKind::PyStart,
            EventKind::PyReturn,
            EventKind::Line,
            EventKind::Raise,
            EventKind::Reraise,
            EventKind::PyUnwind,
        ] {
            assert_eq!(EventKind::from_u8(k as u8), Some(k));
        }
        assert_eq!(EventKind::from_u8(0), None);
        assert_eq!(EventKind::from_u8(99), None);
    }

    #[test]
    fn event_msgpack_roundtrip() {
        let e = Event::new(EventKind::Line, 3, 87, 0xDEAD_BEEF, 42);
        let bytes = crate::to_msgpack(&e).unwrap();
        let back: Event = crate::from_msgpack(&bytes).unwrap();
        assert_eq!(e, back);
    }
}
