use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceFile {
    pub filename: String,
    pub sha1: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExceptionLink {
    pub exc_type: String,
    pub message: String,

    pub relation: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FrameInfo {
    pub file: String,
    pub qualname: String,

    pub lineno: u32,

    pub first_lineno: u32,

    pub locals: Vec<(String, u64)>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObjectItem {
    pub key: Option<String>,

    pub value_id: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObjectNode {
    pub id: u64,

    pub kind: String,

    pub repr: Option<String>,

    pub type_name: Option<String>,

    pub length: Option<u64>,

    pub truncated: bool,
    pub items: Vec<ObjectItem>,
}

impl ObjectNode {
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
