use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MutationValue {
    pub kind: String,

    pub repr: Option<String>,

    pub type_name: Option<String>,

    pub length: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mutation {
    pub seq: u64,

    pub kind: String,

    pub name: String,

    pub key: Option<String>,

    pub value: MutationValue,

    pub file: String,

    pub qualname: String,

    pub line: u32,

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
