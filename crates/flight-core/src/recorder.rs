use std::cell::Cell;
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use flight_format::{CodeInfo, Event, EventKind, RingPayload};

use crate::ring::Ring;


#[derive(Default)]
struct IntHasher(u64);

impl Hasher for IntHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write_u64(&mut self, n: u64) {
        self.0 = n.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
    #[inline]
    fn write_u16(&mut self, n: u16) {
        self.0 = (n as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
    fn write(&mut self, bytes: &[u8]) {

        for &b in bytes {
            self.0 = (self.0 ^ b as u64).wrapping_mul(0x0100_0000_01B3);
        }
    }
}

type IntMap<K, V> = HashMap<K, V, BuildHasherDefault<IntHasher>>;


#[derive(Clone, Copy)]
struct Slot {
    rec_id: u64,
    generation: u64,
    thread: u16,
    ring: *const Ring,
}

thread_local! {
    static SLOT: Cell<Option<Slot>> = const { Cell::new(None) };
}

static NEXT_RECORDER_ID: AtomicU64 = AtomicU64::new(1);


pub struct Recorder {
    id: u64,

    generation: AtomicU64,
    clock: AtomicU64,
    ring_cap: usize,
    next_thread: AtomicU64,
    rings: Mutex<IntMap<u16, Box<Ring>>>,
    codes: Mutex<IntMap<u64, CodeInfo>>,

    interesting: Mutex<IntMap<u64, bool>>,

    deny: Mutex<Vec<String>>,

    force: Mutex<Vec<String>>,
}

impl Recorder {
    pub fn new(ring_cap: usize) -> Recorder {
        Recorder {
            id: NEXT_RECORDER_ID.fetch_add(1, Ordering::Relaxed),
            generation: AtomicU64::new(1),
            clock: AtomicU64::new(0),
            ring_cap,
            next_thread: AtomicU64::new(0),
            rings: Mutex::new(IntMap::default()),
            codes: Mutex::new(IntMap::default()),
            interesting: Mutex::new(IntMap::default()),
            deny: Mutex::new(Vec::new()),
            force: Mutex::new(Vec::new()),
        }
    }


    fn make_thread_ring(&self) -> (u16, *const Ring) {
        let tid = (self.next_thread.fetch_add(1, Ordering::Relaxed) & 0xFFFF) as u16;
        let mut rings = self.rings.lock().unwrap();
        let ring = rings
            .entry(tid)
            .or_insert_with(|| Box::new(Ring::new(self.ring_cap)));
        (tid, &**ring as *const Ring)
    }


    #[inline]
    pub fn record(&self, kind: EventKind, code_id: u64, line: u32) {
        let tstamp = self.clock.fetch_add(1, Ordering::Relaxed);
        let generation = self.generation.load(Ordering::Relaxed);
        SLOT.with(|cell| {
            let slot = match cell.get() {
                Some(s) if s.rec_id == self.id && s.generation == generation => s,
                _ => {
                    let (thread, ring) = self.make_thread_ring();
                    let s = Slot {
                        rec_id: self.id,
                        generation,
                        thread,
                        ring,
                    };
                    cell.set(Some(s));
                    s
                }
            };


            unsafe {
                (*slot.ring).push(Event::new(kind, slot.thread, line, code_id, tstamp));
            }
        });
    }


    pub fn set_filter(&self, deny: Vec<String>, force: Vec<String>) {
        *self.deny.lock().unwrap() = deny;
        *self.force.lock().unwrap() = force;
        self.interesting.lock().unwrap().clear();
    }


    pub fn interesting_cached(&self, code_id: u64) -> Option<bool> {
        self.interesting.lock().unwrap().get(&code_id).copied()
    }


    pub fn decide_interesting(&self, code_id: u64, filename: &str) -> bool {
        let value = self.compute_interesting(filename);
        self.interesting.lock().unwrap().insert(code_id, value);
        value
    }

    fn compute_interesting(&self, filename: &str) -> bool {
        if filename.is_empty() || filename.starts_with('<') {
            return false;
        }
        let real = std::fs::canonicalize(filename)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|_| filename.to_string());
        if self
            .force
            .lock()
            .unwrap()
            .iter()
            .any(|f| real.contains(f.as_str()))
        {
            return true;
        }
        !self
            .deny
            .lock()
            .unwrap()
            .iter()
            .any(|d| real.starts_with(d.as_str()))
    }


    pub fn register_code(&self, code_id: u64, file: &str, qualname: &str, first_line: u32) -> bool {
        let mut codes = self.codes.lock().unwrap();
        if codes.contains_key(&code_id) {
            return false;
        }
        codes.insert(
            code_id,
            CodeInfo {
                file: file.to_string(),
                qualname: qualname.to_string(),
                first_line,
            },
        );
        true
    }


    pub fn generation(&self) -> u64 {
        self.generation.load(Ordering::Relaxed)
    }

    pub fn total_events(&self) -> u64 {
        self.clock.load(Ordering::Relaxed)
    }

    pub fn thread_count(&self) -> usize {
        self.rings.lock().unwrap().len()
    }

    pub fn code_count(&self) -> usize {
        self.codes.lock().unwrap().len()
    }


    pub fn snapshot_ring(&self) -> RingPayload {
        let mut events = Vec::new();
        let mut wrapped = false;
        {
            let rings = self.rings.lock().unwrap();
            for ring in rings.values() {
                let (mut evs, w) = ring.drain();
                wrapped |= w;
                events.append(&mut evs);
            }
        }
        events.sort_by_key(|e| e.tstamp);

        let codes = self.codes.lock().unwrap();
        let mut used = HashMap::new();
        for e in &events {
            if let Some(info) = codes.get(&e.code_id) {
                used.entry(e.code_id).or_insert_with(|| info.clone());
            }
        }
        RingPayload {
            codes: used,
            events,
            wrapped,
        }
    }


    pub fn reset(&self) {
        self.rings.lock().unwrap().clear();
        self.codes.lock().unwrap().clear();
        self.interesting.lock().unwrap().clear();
        self.clock.store(0, Ordering::Relaxed);
        self.next_thread.store(0, Ordering::Relaxed);
        self.generation.fetch_add(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn records_and_snapshots_in_logical_order() {
        let rec = Recorder::new(1024);
        rec.register_code(1, "app.py", "main", 1);
        rec.record(EventKind::PyStart, 1, 0);
        rec.record(EventKind::Line, 1, 10);
        rec.record(EventKind::Line, 1, 11);

        let snap = rec.snapshot_ring();
        assert_eq!(snap.events.len(), 3);
        assert!(!snap.wrapped);
        assert!(snap.events.windows(2).all(|w| w[0].tstamp < w[1].tstamp));
        assert_eq!(snap.codes.len(), 1);
        assert_eq!(snap.codes[&1].qualname, "main");
    }

    #[test]
    fn register_code_is_first_write_wins() {
        let rec = Recorder::new(64);
        assert!(rec.register_code(7, "a.py", "f", 1));
        assert!(!rec.register_code(7, "a.py", "f", 1));
        assert_eq!(rec.code_count(), 1);
    }

    #[test]
    fn snapshot_only_includes_referenced_codes() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "used", 1);
        rec.register_code(2, "b.py", "unused", 1);
        rec.record(EventKind::Line, 1, 5);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.codes.len(), 1);
        assert!(snap.codes.contains_key(&1));
        assert!(!snap.codes.contains_key(&2));
    }

    #[test]
    fn distinct_os_threads_get_distinct_rings() {

        let rec = Arc::new(Recorder::new(64));
        let mut handles = Vec::new();
        for _ in 0..2 {
            let r = Arc::clone(&rec);
            handles.push(std::thread::spawn(move || {
                r.record(EventKind::Line, 1, 1);
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(rec.thread_count(), 2);
        assert_eq!(rec.total_events(), 2);
    }

    #[test]
    fn reset_clears_everything() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "f", 1);
        rec.record(EventKind::Line, 1, 1);
        rec.reset();
        assert_eq!(rec.total_events(), 0);
        assert_eq!(rec.thread_count(), 0);
        assert_eq!(rec.code_count(), 0);
        assert!(rec.snapshot_ring().events.is_empty());

        rec.record(EventKind::Line, 1, 2);
        assert_eq!(rec.total_events(), 1);
        assert_eq!(rec.snapshot_ring().events.len(), 1);
    }

    #[test]
    fn interesting_policy_denies_prefixes_and_synthetic_files() {
        let rec = Recorder::new(64);
        rec.set_filter(vec!["/usr/lib/python".to_string()], vec![]);
        assert!(!rec.compute_interesting("<string>"));
        assert!(!rec.compute_interesting(""));
        assert!(!rec.compute_interesting("/usr/lib/python3.13/json/__init__.py"));


        assert!(rec.compute_interesting("/home/me/project/app.py"));
    }

    #[test]
    fn interesting_decision_is_cached() {
        let rec = Recorder::new(64);
        rec.set_filter(vec![], vec![]);
        assert_eq!(rec.interesting_cached(42), None);
        rec.decide_interesting(42, "/home/me/app.py");
        assert_eq!(rec.interesting_cached(42), Some(true));
    }
}

#[cfg(test)]
mod recorder_ext {
    use super::*;
    use std::sync::Arc;


    #[test]
    fn register_code_returns_true_first_time_false_after() {
        let rec = Recorder::new(64);
        assert!(rec.register_code(10, "a.py", "f", 1));
        assert!(!rec.register_code(10, "a.py", "f", 1));
        assert!(!rec.register_code(10, "different.py", "g", 99));
    }

    #[test]
    fn register_code_first_write_wins_on_metadata() {
        let rec = Recorder::new(64);
        rec.register_code(10, "first.py", "first", 1);
        rec.register_code(10, "second.py", "second", 2);
        rec.record(EventKind::Line, 10, 1);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.codes[&10].file, "first.py");
        assert_eq!(snap.codes[&10].qualname, "first");
        assert_eq!(snap.codes[&10].first_line, 1);
    }

    #[test]
    fn distinct_code_ids_are_all_registered() {
        let rec = Recorder::new(64);
        for id in 0..50 {
            assert!(rec.register_code(id, "a.py", "f", 1));
        }
        assert_eq!(rec.code_count(), 50);
    }


    #[test]
    fn fresh_recorder_counters_are_zero() {
        let rec = Recorder::new(64);
        assert_eq!(rec.total_events(), 0);
        assert_eq!(rec.thread_count(), 0);
        assert_eq!(rec.code_count(), 0);
        assert_eq!(rec.generation(), 1);
    }

    #[test]
    fn total_events_counts_every_record() {
        let rec = Recorder::new(1024);
        for _ in 0..123 {
            rec.record(EventKind::Line, 1, 1);
        }
        assert_eq!(rec.total_events(), 123);
    }

    #[test]
    fn single_thread_makes_one_ring() {
        let rec = Recorder::new(64);
        rec.record(EventKind::Line, 1, 1);
        rec.record(EventKind::Line, 1, 2);
        assert_eq!(rec.thread_count(), 1);
    }


    #[test]
    fn snapshot_events_are_sorted_by_tstamp() {
        let rec = Recorder::new(1024);
        rec.register_code(1, "a.py", "f", 1);
        for i in 0..50 {
            rec.record(EventKind::Line, 1, 100 + i);
        }
        let snap = rec.snapshot_ring();
        assert_eq!(snap.events.len(), 50);
        assert!(snap.events.windows(2).all(|w| w[0].tstamp < w[1].tstamp));
    }

    #[test]
    fn snapshot_records_event_kind_and_line() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "f", 1);
        rec.record(EventKind::PyStart, 1, 0);
        rec.record(EventKind::Line, 1, 7);
        rec.record(EventKind::Raise, 1, 8);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.events[0].kind, EventKind::PyStart as u8);
        assert_eq!(snap.events[1].kind, EventKind::Line as u8);
        assert_eq!(snap.events[1].line, 7);
        assert_eq!(snap.events[2].kind, EventKind::Raise as u8);
    }

    #[test]
    fn snapshot_only_includes_referenced_codes() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "used", 1);
        rec.register_code(2, "b.py", "unused", 1);
        rec.register_code(3, "c.py", "alsounused", 1);
        rec.record(EventKind::Line, 1, 5);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.codes.len(), 1);
        assert!(snap.codes.contains_key(&1));
    }

    #[test]
    fn snapshot_includes_all_referenced_codes() {
        let rec = Recorder::new(1024);
        rec.register_code(1, "a.py", "a", 1);
        rec.register_code(2, "b.py", "b", 1);
        rec.record(EventKind::Line, 1, 1);
        rec.record(EventKind::Line, 2, 1);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.codes.len(), 2);
    }

    #[test]
    fn snapshot_event_for_unregistered_code_has_no_code_entry() {
        let rec = Recorder::new(64);
        rec.record(EventKind::Line, 999, 1);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.events.len(), 1);
        assert!(snap.codes.is_empty());
    }

    #[test]
    fn empty_recorder_snapshot_is_empty() {
        let rec = Recorder::new(64);
        let snap = rec.snapshot_ring();
        assert!(snap.events.is_empty());
        assert!(snap.codes.is_empty());
        assert!(!snap.wrapped);
    }

    #[test]
    fn snapshot_reports_wrapped_when_ring_overflows() {
        let rec = Recorder::new(16);
        rec.register_code(1, "a.py", "f", 1);
        for i in 0..100 {
            rec.record(EventKind::Line, 1, i);
        }
        let snap = rec.snapshot_ring();
        assert!(snap.wrapped);
        assert_eq!(snap.events.len(), 16);

        assert_eq!(rec.total_events(), 100);
    }


    #[test]
    fn reset_bumps_generation() {
        let rec = Recorder::new(64);
        let g0 = rec.generation();
        rec.reset();
        assert_eq!(rec.generation(), g0 + 1);
        rec.reset();
        assert_eq!(rec.generation(), g0 + 2);
    }

    #[test]
    fn reset_clears_all_state() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "f", 1);
        rec.set_filter(vec![], vec![]);
        rec.decide_interesting(1, "/home/x.py");
        for _ in 0..10 {
            rec.record(EventKind::Line, 1, 1);
        }
        rec.reset();
        assert_eq!(rec.total_events(), 0);
        assert_eq!(rec.thread_count(), 0);
        assert_eq!(rec.code_count(), 0);
        assert!(rec.snapshot_ring().events.is_empty());
        assert_eq!(rec.interesting_cached(1), None);
    }

    #[test]
    fn recording_works_again_after_reset() {
        let rec = Recorder::new(64);
        rec.record(EventKind::Line, 1, 1);
        rec.reset();
        rec.record(EventKind::Line, 1, 2);
        rec.record(EventKind::Line, 1, 3);
        assert_eq!(rec.total_events(), 2);
        assert_eq!(rec.snapshot_ring().events.len(), 2);
    }


    #[test]
    fn synthetic_and_empty_filenames_are_never_interesting() {
        let rec = Recorder::new(64);
        rec.set_filter(vec![], vec![]);
        assert!(!rec.compute_interesting(""));
        assert!(!rec.compute_interesting("<string>"));
        assert!(!rec.compute_interesting("<frozen importlib._bootstrap>"));
    }

    #[test]
    fn no_deny_list_makes_real_paths_interesting() {
        let rec = Recorder::new(64);
        rec.set_filter(vec![], vec![]);
        assert!(rec.compute_interesting("/home/me/project/app.py"));
    }

    #[test]
    fn denied_prefix_is_not_interesting() {
        let rec = Recorder::new(64);
        rec.set_filter(vec!["/usr/lib/python".to_string()], vec![]);
        assert!(!rec.compute_interesting("/usr/lib/python3.13/json/__init__.py"));
        assert!(rec.compute_interesting("/home/me/app.py"));
    }

    #[test]
    fn force_include_overrides_deny() {
        let rec = Recorder::new(64);
        rec.set_filter(
            vec!["/usr/lib/python".to_string()],
            vec!["mypackage".to_string()],
        );

        assert!(rec.compute_interesting("/usr/lib/python3.13/site-packages/mypackage/x.py"));
    }

    #[test]
    fn set_filter_clears_cached_decisions() {
        let rec = Recorder::new(64);
        rec.set_filter(vec![], vec![]);
        rec.decide_interesting(1, "/home/me/app.py");
        assert_eq!(rec.interesting_cached(1), Some(true));
        rec.set_filter(vec!["/home/me".to_string()], vec![]);
        assert_eq!(rec.interesting_cached(1), None, "cache cleared on set_filter");
    }

    #[test]
    fn decide_interesting_caches_the_result() {
        let rec = Recorder::new(64);
        rec.set_filter(vec!["/deny".to_string()], vec![]);
        assert_eq!(rec.interesting_cached(5), None);
        let d = rec.decide_interesting(5, "/deny/x.py");
        assert!(!d);
        assert_eq!(rec.interesting_cached(5), Some(false));
    }

    #[test]
    fn decide_interesting_returns_the_computed_value() {
        let rec = Recorder::new(64);
        rec.set_filter(vec![], vec![]);
        assert!(rec.decide_interesting(1, "/home/me/app.py"));
        assert!(!rec.decide_interesting(2, "<string>"));
    }


    #[test]
    fn concurrent_threads_get_distinct_rings_and_all_events_survive() {
        let rec = Arc::new(Recorder::new(4096));
        let mut handles = Vec::new();
        for _ in 0..6 {
            let r = Arc::clone(&rec);
            handles.push(std::thread::spawn(move || {
                for _ in 0..200 {
                    r.record(EventKind::Line, 1, 1);
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(rec.thread_count(), 6);
        assert_eq!(rec.total_events(), 6 * 200);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.events.len(), 6 * 200);

        assert!(snap.events.windows(2).all(|w| w[0].tstamp < w[1].tstamp));
    }

    #[test]
    fn logical_clock_is_shared_and_dense_across_threads() {
        let rec = Arc::new(Recorder::new(8192));
        let mut handles = Vec::new();
        for _ in 0..4 {
            let r = Arc::clone(&rec);
            handles.push(std::thread::spawn(move || {
                for _ in 0..500 {
                    r.record(EventKind::Line, 1, 1);
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let snap = rec.snapshot_ring();
        assert_eq!(snap.events.len(), 2000);
        assert!(snap.events.windows(2).all(|w| w[0].tstamp + 1 == w[1].tstamp));
    }

    #[test]
    fn distinct_recorders_have_distinct_ids() {


        let a = Recorder::new(64);
        let b = Recorder::new(64);
        a.record(EventKind::Line, 1, 1);
        b.record(EventKind::Line, 1, 1);
        b.record(EventKind::Line, 1, 1);
        assert_eq!(a.total_events(), 1);
        assert_eq!(b.total_events(), 2);
        assert_eq!(a.snapshot_ring().events.len(), 1);
        assert_eq!(b.snapshot_ring().events.len(), 2);
    }
}
