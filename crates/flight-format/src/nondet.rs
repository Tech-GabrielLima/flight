//! Phase-3 deterministic replay: the NONDET block (0x09).
//!
//! A program is a deterministic function of its non-deterministic inputs — the
//! clock, randomness, uuids, the environment (VISION.md §4.1). Record *only*
//! those, at the edges (cheap), and the whole run can be replayed bit-for-bit
//! by feeding the recorded values back in order — the `rr` model at the level
//! of Python APIs.
//!
//! Each [`NonDetEvent`] is one interposed call's result. The value is a `(tag,
//! payload)` string pair — Python owns the encoding (a float's repr, bytes as
//! hex, a dict as JSON…), so this crate just persists it. `source` identifies
//! the boundary (`"time.time"`, `"random.random"`, …) and lets the replayer
//! detect divergence when the code calls a different boundary than recorded.

use serde::{Deserialize, Serialize};

/// One recorded result of a non-deterministic call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NonDetEvent {
    /// Order in which the call happened (monotonic).
    pub seq: u64,
    /// The interposed boundary, e.g. `"time.time"` or `"random.random"`.
    pub source: String,
    /// Value type tag understood by the Python codec (`"f"`, `"i"`, `"b"`, …).
    pub tag: String,
    /// Encoded value payload (decoded by the Python codec on replay).
    pub payload: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nondet_roundtrips_msgpack() {
        let events = vec![
            NonDetEvent {
                seq: 0,
                source: "time.time".into(),
                tag: "f".into(),
                payload: "1783000000.5".into(),
            },
            NonDetEvent {
                seq: 1,
                source: "random.random".into(),
                tag: "f".into(),
                payload: "0.37444887".into(),
            },
            NonDetEvent {
                seq: 2,
                source: "os.urandom".into(),
                tag: "b".into(),
                payload: "deadbeef".into(),
            },
        ];
        let bytes = crate::to_msgpack(&events).unwrap();
        let back: Vec<NonDetEvent> = crate::from_msgpack(&bytes).unwrap();
        assert_eq!(events, back);
    }
}
