//! Phase-1 crash payloads: the block bodies that turn a bare event ring into a
//! full black box — source code, the exception chain, the stack frames, and the
//! serialized object graph of every local.
//!
//! These structs are the on-disk schema. The Python engine builds them at crash
//! time (via `flight._core.dump_crash`); the reader decodes them back. The
//! *walk* of live Python objects lives in Python (it runs once, in a doomed
//! process — see TECHNICAL.md §1.6); this file only defines what gets written.

use serde::{Deserialize, Serialize};

/// One source file involved in the crash (SOURCE block, 0x02).
///
/// Carrying the text means the viewer shows code even on another machine
/// (VISION.md §8). `sha1` lets a viewer dedupe / verify against a local copy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceFile {
    pub filename: String,
    pub sha1: String,
    pub text: String,
}

/// One link in the exception chain (EXCEPTION block, 0x03).
///
/// Ordered most-recent-first: entry 0 is the exception that reached the hook,
/// followed by its `__cause__` / `__context__` ancestry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExceptionLink {
    pub exc_type: String,
    pub message: String,
    /// How this entry relates to the *previous* one:
    /// `"head"` (the raised exception), `"cause"` (explicit `raise … from …`),
    /// or `"context"` (implicit, during handling of another).
    pub relation: String,
}

/// One stack frame (FRAME block, 0x04).
///
/// Frames are ordered crash-first: frame 0 is where the exception was raised,
/// then its callers outward. Each local maps a name to a node id in the object
/// graph, so two frames sharing an object point at the *same* id (aliasing).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FrameInfo {
    pub file: String,
    pub qualname: String,
    /// Line currently executing in this frame.
    pub lineno: u32,
    /// First line of the function (for the viewer to anchor the source).
    pub first_lineno: u32,
    /// `(local name, object id)` pairs.
    pub locals: Vec<(String, u64)>,
}

/// One reference from a container/object to a child value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObjectItem {
    /// Dict key or attribute name; `None` for list/tuple/set elements.
    pub key: Option<String>,
    /// Id of the referenced node.
    pub value_id: u64,
}

/// One node of the serialized object graph (OBJECT block, 0x05).
///
/// Every value — even a scalar — is a node with a stable `id`, so identity and
/// aliasing are preserved uniformly. Containers and objects reference their
/// children by id through `items`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObjectNode {
    pub id: u64,
    /// Discriminator the viewer renders on: `"int"`, `"float"`, `"str"`,
    /// `"bytes"`, `"bool"`, `"none"`, `"dict"`, `"list"`, `"tuple"`, `"set"`,
    /// `"object"`, `"redacted"`, `"truncated"`, or an adapter kind.
    pub kind: String,
    /// A human-facing rendering: the value for scalars, `safe_repr` for objects.
    pub repr: Option<String>,
    /// Class qualname for `"object"` / adapter nodes.
    pub type_name: Option<String>,
    /// Real length before any container/string truncation.
    pub length: Option<u64>,
    /// True if this node was cut short (container/string clipped, depth/budget
    /// exceeded, or a never-expanded placeholder).
    pub truncated: bool,
    pub items: Vec<ObjectItem>,
}

impl ObjectNode {
    /// A minimal placeholder for a value that was referenced but never
    /// expanded (budget/deadline hit). Guarantees every referenced id resolves.
    pub fn placeholder(id: u64) -> ObjectNode {
        ObjectNode {
            id,
            kind: "truncated".to_string(),
            repr: Some("<truncated>".to_string()),
            type_name: None,
            length: None,
            truncated: true,
            items: Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crash_payloads_roundtrip_msgpack() {
        let sources = vec![SourceFile {
            filename: "app.py".into(),
            sha1: "abc".into(),
            text: "x = 1\n".into(),
        }];
        let excs = vec![
            ExceptionLink {
                exc_type: "ValueError".into(),
                message: "bad".into(),
                relation: "head".into(),
            },
            ExceptionLink {
                exc_type: "KeyError".into(),
                message: "'k'".into(),
                relation: "context".into(),
            },
        ];
        let frames = vec![FrameInfo {
            file: "app.py".into(),
            qualname: "main".into(),
            lineno: 3,
            first_lineno: 1,
            locals: vec![("x".into(), 0), ("cfg".into(), 1)],
        }];
        let objects = vec![
            ObjectNode {
                id: 0,
                kind: "int".into(),
                repr: Some("42".into()),
                type_name: None,
                length: None,
                truncated: false,
                items: vec![],
            },
            ObjectNode {
                id: 1,
                kind: "dict".into(),
                repr: None,
                type_name: None,
                length: Some(1),
                truncated: false,
                items: vec![ObjectItem {
                    key: Some("k".into()),
                    value_id: 0,
                }],
            },
        ];

        assert_eq!(rt(&sources), sources);
        assert_eq!(rt(&excs), excs);
        assert_eq!(rt(&frames), frames);
        assert_eq!(rt(&objects), objects);
    }

    fn rt<T>(v: &T) -> T
    where
        T: serde::Serialize + serde::de::DeserializeOwned,
    {
        crate::from_msgpack(&crate::to_msgpack(v).unwrap()).unwrap()
    }

    #[test]
    fn placeholder_is_truncated() {
        let p = ObjectNode::placeholder(9);
        assert_eq!(p.id, 9);
        assert!(p.truncated);
        assert_eq!(p.kind, "truncated");
    }
}
