use std::cmp::{max, min};

/// Maintains an exact, coalesced set of downloaded byte intervals [start, end] (inclusive).
#[derive(Debug, Clone)]
pub struct IntervalJournal {
    pub file_size: u64,
    pub intervals: Vec<(u64, u64)>,
}

impl IntervalJournal {
    pub fn new(file_size: u64) -> Self {
        Self {
            file_size,
            intervals: Vec::new(),
        }
    }

    pub fn from_intervals(file_size: u64, intervals: Vec<(u64, u64)>) -> Self {
        let mut j = Self::new(file_size);
        for (s, e) in intervals {
            j.add_range(s, e);
        }
        j
    }

    pub fn add_range(&mut self, start: u64, end: u64) {
        if self.file_size == 0 {
            return;
        }
        let mut s = start;
        let mut e = min(self.file_size - 1, end);
        if s > e {
            return;
        }

        let mut merged = Vec::with_capacity(self.intervals.len() + 1);
        let mut inserted = false;

        for &(cur_s, cur_e) in &self.intervals {
            if cur_e.saturating_add(1) < s {
                merged.push((cur_s, cur_e));
            } else if e.saturating_add(1) < cur_s {
                if !inserted {
                    merged.push((s, e));
                    inserted = true;
                }
                merged.push((cur_s, cur_e));
            } else {
                s = min(s, cur_s);
                e = max(e, cur_e);
            }
        }

        if !inserted {
            merged.push((s, e));
        }
        self.intervals = merged;
    }

    pub fn total_downloaded(&self) -> u64 {
        self.intervals.iter().map(|&(s, e)| e - s + 1).sum()
    }

    pub fn is_complete(&self) -> bool {
        self.file_size > 0
            && self.intervals.len() == 1
            && self.intervals[0] == (0, self.file_size - 1)
    }

    pub fn missing_ranges(&self) -> Vec<(u64, u64)> {
        if self.file_size == 0 {
            return Vec::new();
        }
        let mut holes = Vec::new();
        let mut cursor = 0u64;
        for &(s, e) in &self.intervals {
            if cursor < s {
                holes.push((cursor, s - 1));
            }
            cursor = max(cursor, e.saturating_add(1));
        }
        if cursor < self.file_size {
            holes.push((cursor, self.file_size - 1));
        }
        holes
    }

    pub fn plan_hole_slices(&self, max_slices: usize) -> Vec<(u64, u64)> {
        let holes = self.missing_ranges();
        if holes.is_empty() {
            return Vec::new();
        }
        let max_slices = max(1, max_slices);
        let total_missing: u64 = holes.iter().map(|&(s, e)| e - s + 1).sum();
        let min_chunk = 256 * 1024u64;
        let target_chunk = max(
            min_chunk,
            total_missing.div_ceil(max_slices as u64),
        );

        let mut slices = Vec::new();
        for (h_start, h_end) in holes {
            let mut cur = h_start;
            while cur <= h_end {
                if slices.len() >= max_slices - 1 {
                    slices.push((cur, h_end));
                    break;
                }
                let mut nxt_end = min(h_end, cur + target_chunk - 1);
                if h_end - nxt_end < min_chunk {
                    nxt_end = h_end;
                }
                slices.push((cur, nxt_end));
                cur = nxt_end + 1;
            }
        }
        slices
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_coalesce_and_holes() {
        let mut j = IntervalJournal::new(1000);
        j.add_range(0, 99);
        j.add_range(100, 199);
        j.add_range(500, 599);
        assert_eq!(j.intervals, vec![(0, 199), (500, 599)]);
        assert_eq!(j.total_downloaded(), 300);
        assert_eq!(j.missing_ranges(), vec![(200, 499), (600, 999)]);
        assert!(!j.is_complete());

        j.add_range(200, 499);
        j.add_range(600, 999);
        assert!(j.is_complete());
    }
}
