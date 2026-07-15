use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HeaderMeta {
    pub tool: String,

    pub flight_version: String,

    pub created_unix_ms: u64,
}

impl HeaderMeta {
    pub fn new(flight_version: &str) -> Self {
        let created_unix_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        HeaderMeta {
            tool: "flight".to_string(),
            flight_version: flight_version.to_string(),
            created_unix_ms,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_meta_is_a_msgpack_map_and_tolerates_new_fields() {
        #[derive(Serialize)]
        struct Future<'a> {
            tool: &'a str,
            flight_version: &'a str,
            created_unix_ms: u64,
            some_new_field: bool,
        }
        let bytes = rmp_serde::to_vec_named(&Future {
            tool: "flight",
            flight_version: "9.9.9",
            created_unix_ms: 123,
            some_new_field: true,
        })
        .unwrap();

        let meta: HeaderMeta = rmp_serde::from_slice(&bytes).unwrap();
        assert_eq!(meta.flight_version, "9.9.9");
        assert_eq!(meta.created_unix_ms, 123);
    }
}
