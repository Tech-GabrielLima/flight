use serde::{Deserialize, Serialize};


#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NonDetEvent {

    pub seq: u64,

    pub source: String,

    pub tag: String,

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
