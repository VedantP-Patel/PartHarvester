use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Lock-free positional writer:
/// - Linux/macOS: `std::os::unix::fs::FileExt::write_at` (`pwrite` syscall)
/// - Windows: `std::os::windows::fs::FileExt::seek_write` (`WriteFile` with `OVERLAPPED`)
pub struct PositionalWriter {
    file: File,
}

impl PositionalWriter {
    pub fn open(path: &Path) -> io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        Ok(Self { file })
    }

    pub fn write_at(&self, offset: u64, data: &[u8]) -> io::Result<usize> {
        #[cfg(windows)]
        {
            use std::os::windows::fs::FileExt;
            let mut written = 0usize;
            while written < data.len() {
                let n = self.file.seek_write(&data[written..], offset + written as u64)?;
                if n == 0 {
                    return Err(io::Error::new(io::ErrorKind::WriteZero, "seek_write returned 0"));
                }
                written += n;
            }
            Ok(written)
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::FileExt;
            let mut written = 0usize;
            while written < data.len() {
                let n = self.file.write_at(&data[written..], offset + written as u64)?;
                if n == 0 {
                    return Err(io::Error::new(io::ErrorKind::WriteZero, "write_at returned 0"));
                }
                written += n;
            }
            Ok(written)
        }
    }

    pub fn sync_all(&self) -> io::Result<()> {
        self.file.sync_all()
    }
}

/// Preallocates a sparse file of `size` bytes on Windows (NTFS FSCTL_SET_SPARSE) or Linux (`set_len`).
pub fn preallocate_sparse_file(path: &Path, size: u64) -> io::Result<()> {
    if size == 0 {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let exists = path.exists();
    let current_len = if exists {
        std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)
    } else {
        0
    };

    if !exists || current_len < size {
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(path)?;

        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows::Win32::Foundation::HANDLE;
            use windows::Win32::System::Ioctl::FSCTL_SET_SPARSE;
            use windows::Win32::System::IO::DeviceIoControl;

            let handle = HANDLE(file.as_raw_handle() as _);
            let mut bytes_returned: u32 = 0;
            unsafe {
                let _ = DeviceIoControl(
                    handle,
                    FSCTL_SET_SPARSE,
                    None,
                    0,
                    None,
                    0,
                    Some(&mut bytes_returned),
                    None,
                );
            }
        }

        if let Err(e) = file.set_len(size) {
            if size > 4_294_967_295 {
                return Err(io::Error::new(
                    e.kind(),
                    format!("{e} (hint: if saving to a FAT32 drive, files >4 GB are unsupported; use NTFS/exFAT/ext4)"),
                ));
            }
            return Err(e);
        }
    }
    Ok(())
}

/// Recovers physically allocated byte ranges from a sparse file after a crash:
/// - Windows: `FSCTL_QUERY_ALLOCATED_RANGES`
/// - Linux: `lseek(SEEK_DATA)` / `lseek(SEEK_HOLE)`
pub fn query_allocated_ranges(path: &Path, file_size: u64) -> Vec<(u64, u64)> {
    if file_size == 0 || !path.exists() {
        return Vec::new();
    }

    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows::Win32::Foundation::{ERROR_MORE_DATA, HANDLE};
        use windows::Win32::System::Ioctl::{
            FILE_ALLOCATED_RANGE_BUFFER, FSCTL_QUERY_ALLOCATED_RANGES,
        };
        use windows::Win32::System::IO::DeviceIoControl;

        let Ok(file) = File::open(path) else {
            return Vec::new();
        };
        let handle = HANDLE(file.as_raw_handle() as _);
        let mut ranges = Vec::new();
        let mut query = FILE_ALLOCATED_RANGE_BUFFER {
            FileOffset: 0,
            Length: file_size as i64,
        };
        let mut out_buf = vec![
            FILE_ALLOCATED_RANGE_BUFFER {
                FileOffset: 0,
                Length: 0,
            };
            512
        ];

        loop {
            let mut bytes_returned: u32 = 0;
            let res = unsafe {
                DeviceIoControl(
                    handle,
                    FSCTL_QUERY_ALLOCATED_RANGES,
                    Some(&query as *const _ as *const _),
                    std::mem::size_of::<FILE_ALLOCATED_RANGE_BUFFER>() as u32,
                    Some(out_buf.as_mut_ptr() as *mut _),
                    (out_buf.len() * std::mem::size_of::<FILE_ALLOCATED_RANGE_BUFFER>()) as u32,
                    Some(&mut bytes_returned),
                    None,
                )
            };

            let count = bytes_returned as usize / std::mem::size_of::<FILE_ALLOCATED_RANGE_BUFFER>();
            for item in out_buf.iter().take(count) {
                if item.Length <= 0 {
                    continue;
                }
                let off = item.FileOffset as u64;
                let len = item.Length as u64;
                let end = (off + len - 1).min(file_size - 1);
                if end == file_size - 1 && len <= 65536 && off > 0 {
                    continue;
                }
                ranges.push((off, end));
            }

            let more = match res {
                Ok(()) => false,
                Err(e) => e.code() == ERROR_MORE_DATA.to_hresult(),
            };
            if !more || count == 0 {
                break;
            }
            let last = &out_buf[count - 1];
            let next_off = (last.FileOffset + last.Length) as u64;
            if next_off >= file_size {
                break;
            }
            query = FILE_ALLOCATED_RANGE_BUFFER {
                FileOffset: next_off as i64,
                Length: (file_size - next_off) as i64,
            };
        }
        ranges
    }

    #[cfg(target_os = "linux")]
    {
        use std::os::unix::io::AsRawFd;
        let Ok(file) = File::open(path) else {
            return Vec::new();
        };
        let fd = file.as_raw_fd();
        let mut ranges = Vec::new();
        let mut cur = 0i64;
        let size_i64 = file_size as i64;
        while cur < size_i64 {
            let data_off = unsafe { libc::lseek(fd, cur, libc::SEEK_DATA) };
            if data_off < 0 || data_off >= size_i64 {
                break;
            }
            let hole_off = unsafe { libc::lseek(fd, data_off, libc::SEEK_HOLE) };
            let end = if hole_off < 0 || hole_off > size_i64 {
                size_i64 - 1
            } else {
                hole_off - 1
            };
            if end >= data_off {
                ranges.push((data_off as u64, end as u64));
            }
            cur = if hole_off <= data_off {
                size_i64
            } else {
                hole_off
            };
        }
        ranges
    }

    #[cfg(all(unix, not(target_os = "linux")))]
    {
        let _ = (path, file_size);
        Vec::new()
    }
}

/// Returns interface names (Linux) or interface indices/IPs that have an active default gateway (0.0.0.0/0).
pub fn get_default_route_interfaces() -> HashSet<String> {
    #[cfg(target_os = "linux")]
    {
        let mut set = HashSet::new();
        if let Ok(contents) = std::fs::read_to_string("/proc/net/route") {
            for line in contents.lines().skip(1) {
                let cols: Vec<&str> = line.split_whitespace().collect();
                if cols.len() >= 4 {
                    let iface = cols[0];
                    let dest = cols[1];
                    let flags = u32::from_str_radix(cols[3], 16).unwrap_or(0);
                    // Destination == 00000000 and RTF_UP (0x1) | RTF_GATEWAY (0x2)
                    if dest == "00000000" && (flags & 0x3) == 0x3 {
                        set.insert(iface.to_string());
                    }
                }
            }
        }
        set
    }

    #[cfg(windows)]
    {
        // On Windows, query IPv4 forward table via `windows` crate for 0.0.0.0/0 routes
        use windows::Win32::NetworkManagement::IpHelper::{
            FreeMibTable, GetIpForwardTable2, MIB_IPFORWARD_TABLE2,
        };
        use windows::Win32::Networking::WinSock::AF_INET;

        let mut set = HashSet::new();
        unsafe {
            let mut table: *mut MIB_IPFORWARD_TABLE2 = std::ptr::null_mut();
            if GetIpForwardTable2(AF_INET, &mut table).is_ok() && !table.is_null() {
                let num = (*table).NumEntries as usize;
                let rows = std::slice::from_raw_parts((*table).Table.as_ptr(), num);
                for row in rows {
                    if row.DestinationPrefix.PrefixLength == 0 {
                        set.insert(row.InterfaceIndex.to_string());
                    }
                }
                FreeMibTable(table as *const _);
            }
        }
        set
    }

    #[cfg(not(any(target_os = "linux", windows)))]
    {
        HashSet::new()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArchiveExtractorKind {
    SevenZip,
    Unrar,
    WinRar,
}

#[derive(Debug, Clone)]
pub struct ArchiveExtractorBin {
    pub kind: ArchiveExtractorKind,
    pub path: PathBuf,
}

/// Locates 7-Zip binary cross-platform (`7z.exe` on Windows, `7zz`/`7z`/`7za` on Linux/macOS).
pub fn find_7zip() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        let mut candidates = vec![
            PathBuf::from(r"C:\Program Files\7-Zip\7z.exe"),
            PathBuf::from(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ];
        if let Ok(local) = std::env::var("LocalAppData") {
            candidates.push(PathBuf::from(&local).join(r"Programs\7-Zip\7z.exe"));
        }
        if let Ok(profile) = std::env::var("USERPROFILE") {
            candidates.push(PathBuf::from(&profile).join(r"scoop\shims\7z.exe"));
        }
        for p in candidates {
            if p.is_file() {
                return Some(p);
            }
        }
    }

    for bin in ["7zz", "7z", "7za"] {
        if let Some(found) = which_in_path(bin) {
            return Some(found);
        }
    }
    None
}

/// Locates WinRAR / UnRAR binary (`UnRAR.exe`, `WinRAR.exe`, `unrar`, `rar`).
pub fn find_winrar_or_unrar() -> Option<ArchiveExtractorBin> {
    #[cfg(windows)]
    {
        let mut roots = vec![
            PathBuf::from(r"C:\Program Files\WinRAR"),
            PathBuf::from(r"C:\Program Files (x86)\WinRAR"),
        ];
        if let Ok(local) = std::env::var("LocalAppData") {
            roots.push(PathBuf::from(local).join(r"Programs\WinRAR"));
        }
        for root in &roots {
            let unrar = root.join("UnRAR.exe");
            if unrar.is_file() {
                return Some(ArchiveExtractorBin {
                    kind: ArchiveExtractorKind::Unrar,
                    path: unrar,
                });
            }
            let rar = root.join("Rar.exe");
            if rar.is_file() {
                return Some(ArchiveExtractorBin {
                    kind: ArchiveExtractorKind::Unrar,
                    path: rar,
                });
            }
            let winrar = root.join("WinRAR.exe");
            if winrar.is_file() {
                return Some(ArchiveExtractorBin {
                    kind: ArchiveExtractorKind::WinRar,
                    path: winrar,
                });
            }
        }
    }

    for (bin, kind) in [
        ("unrar", ArchiveExtractorKind::Unrar),
        ("rar", ArchiveExtractorKind::Unrar),
        ("winrar", ArchiveExtractorKind::WinRar),
    ] {
        if let Some(found) = which_in_path(bin) {
            return Some(ArchiveExtractorBin { kind, path: found });
        }
    }
    None
}

/// Picks the best available extractor (7-Zip, UnRAR, or WinRAR) for the given archive file.
pub fn find_archive_extractor(archive_path: &Path) -> Option<ArchiveExtractorBin> {
    let name_lower = archive_path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let is_rar = name_lower.contains(".rar")
        || name_lower.ends_with(".exe")
        || regex_is_r00(&name_lower);

    if let Some(sz) = find_7zip() {
        return Some(ArchiveExtractorBin {
            kind: ArchiveExtractorKind::SevenZip,
            path: sz,
        });
    }

    if let Some(wr) = find_winrar_or_unrar() {
        // UnRAR/Rar CLI only handles RAR archives; WinRAR.exe handles .rar/.7z/.zip
        if is_rar || wr.kind == ArchiveExtractorKind::WinRar {
            return Some(wr);
        }
        #[cfg(windows)]
        {
            for root in [r"C:\Program Files\WinRAR", r"C:\Program Files (x86)\WinRAR"] {
                let winrar = PathBuf::from(root).join("WinRAR.exe");
                if winrar.is_file() {
                    return Some(ArchiveExtractorBin {
                        kind: ArchiveExtractorKind::WinRar,
                        path: winrar,
                    });
                }
            }
        }
    }
    None
}

fn regex_is_r00(lower_name: &str) -> bool {
    if let Some(dot) = lower_name.rfind('.') {
        let ext = &lower_name[dot + 1..];
        return ext.len() == 3 && ext.starts_with('r') && ext[1..].chars().all(|c| c.is_ascii_digit());
    }
    false
}

fn which_in_path(bin: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{bin}.exe"));
            if exe.is_file() {
                return Some(exe);
            }
        }
    }
    None
}

pub fn open_folder(folder: &Path) -> bool {
    if !folder.exists() {
        return false;
    }
    #[cfg(windows)]
    let res = Command::new("explorer.exe").arg(folder).spawn();
    #[cfg(target_os = "linux")]
    let res = Command::new("xdg-open").arg(folder).spawn();
    #[cfg(target_os = "macos")]
    let res = Command::new("open").arg(folder).spawn();
    res.is_ok()
}

pub fn reveal_file(target: &Path) -> bool {
    if !target.exists() {
        return target.parent().map(open_folder).unwrap_or(false);
    }
    #[cfg(windows)]
    let res = Command::new("explorer.exe")
        .arg(format!("/select,{}", target.display()))
        .spawn();
    #[cfg(target_os = "linux")]
    let res = Command::new("xdg-open")
        .arg(target.parent().unwrap_or(target))
        .spawn();
    #[cfg(target_os = "macos")]
    let res = Command::new("open").arg("-R").arg(target).spawn();
    res.is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_positional_writer_concurrent_offsets() {
        let dir = tempdir().unwrap();
        let file_path = dir.path().join("test.bin");
        preallocate_sparse_file(&file_path, 4096).unwrap();

        let writer = PositionalWriter::open(&file_path).unwrap();
        writer.write_at(1024, b"HELLO").unwrap();
        writer.write_at(0, b"START").unwrap();
        writer.sync_all().unwrap();

        let data = std::fs::read(&file_path).unwrap();
        assert_eq!(&data[0..5], b"START");
        assert_eq!(&data[1024..1029], b"HELLO");
    }
}
