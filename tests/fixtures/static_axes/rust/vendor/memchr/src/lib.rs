//! Vendored by cargo vendor from crates.io, so S leaves it out.
pub fn memchr(needle: u8, haystack: &[u8]) -> Option<usize> {
    haystack.iter().position(|&b| b == needle)
}
