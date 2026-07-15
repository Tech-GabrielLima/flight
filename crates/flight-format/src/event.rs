use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u8)]
pub enum EventKind {
    PyStart = 1,

    PyReturn = 2,

    Line = 3,

    Raise = 4,

    Reraise = 5,

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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(C)]
pub struct Event {
    pub kind: u8,

    pub thread: u16,

    pub line: u32,

    pub code_id: u64,

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
