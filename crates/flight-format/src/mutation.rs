//! Phase-2 scope recording: the MUTATION block (0x07).
//!
//! Inside a `with flight.record():` scope, every state write is captured as a
//! [`Mutation`] — the event-sourcing log of the program's memory (VISION.md
//! §10, TECHNICAL.md §3.1). Replaying the log answers "what was `x` at step t"
//! and "who mutated this container", without ever storing the whole state.
//!
//! Each mutation carries a *shallow* rendering of the new value (kind + repr +
//! type + length), not a deep graph: the log is a sequence of value snapshots,
//! which is exactly what a per-variable history needs and keeps the log small.

use serde::{Deserialize, Serialize};

/// A shallow rendering of a value at the moment it was written.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MutationValue {
    /// Discriminator: `"int"`, `"str"`, `"dict"`, `"object"`, `"redacted"`, …
    pub kind: String,
    /// Human rendering (value for scalars, `safe_repr`/summary otherwise).
    pub repr: Option<String>,
    /// Class qualname for object/container values.
    pub type_name: Option<String>,
    /// Real length for containers/strings.
    pub length: Option<u64>,
}

/// One recorded state write.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mutation {
    /// Logical order within the recording (monotonic).
    pub seq: u64,
    /// `"local"` (a local variable was (re)bound), `"item"` (a container
    /// key/index was written), or `"attr"` (an attribute was set).
    pub kind: String,
    /// Variable name (for `local`) or the watched container's display name.
    pub name: String,
    /// The key/index/attribute written, for `item`/`attr`; `None` for `local`.
    pub key: Option<String>,
    /// Shallow rendering of the newly written value.
    pub value: MutationValue,
    /// Source file where the write happened.
    pub file: String,
    /// Enclosing function qualname.
    pub qualname: String,
    /// Line where the write happened.
    pub line: u32,
    /// Identity of the frame the write happened in (disambiguates recursion).
    pub frame: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mutation_roundtrips_msgpack() {
        let muts = vec![
            Mutation {
                seq: 0,
                kind: "local".into(),
                name: "total".into(),
                key: None,
                value: MutationValue {
                    kind: "int".into(),
                    repr: Some("0".into()),
                    type_name: None,
                    length: None,
                },
                file: "app.py".into(),
                qualname: "run".into(),
                line: 5,
                frame: 140234,
            },
            Mutation {
                seq: 1,
                kind: "item".into(),
                name: "cache".into(),
                key: Some("user".into()),
                value: MutationValue {
                    kind: "str".into(),
                    repr: Some("bob".into()),
                    type_name: None,
                    length: Some(3),
                },
                file: "app.py".into(),
                qualname: "run".into(),
                line: 6,
                frame: 140234,
            },
        ];
        let bytes = crate::to_msgpack(&muts).unwrap();
        let back: Vec<Mutation> = crate::from_msgpack(&bytes).unwrap();
        assert_eq!(muts, back);
    }
}
