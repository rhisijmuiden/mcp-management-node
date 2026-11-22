//go:build windows

package sysinfo

import (
	"strconv"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

var (
	kernel32                 = windows.NewLazySystemDLL("kernel32.dll")
	procGetSystemTimes       = kernel32.NewProc("GetSystemTimes")
	procGlobalMemoryStatusEx = kernel32.NewProc("GlobalMemoryStatusEx")
	procGetTickCount64       = kernel32.NewProc("GetTickCount64")
)

type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

func ftToUint(ft windows.Filetime) uint64 {
	return uint64(ft.HighDateTime)<<32 | uint64(ft.LowDateTime)
}

// readCPU returns idle and total CPU time (100 ns units); kernel time includes idle time.
func readCPU() (idle, total uint64, ok bool) {
	var i, k, u windows.Filetime
	r, _, _ := procGetSystemTimes.Call(uintptr(unsafe.Pointer(&i)), uintptr(unsafe.Pointer(&k)), uintptr(unsafe.Pointer(&u)))
	if r == 0 {
		return 0, 0, false
	}
	return ftToUint(i), ftToUint(k) + ftToUint(u), true
}

func cpuPercent() float64 {
	idle, total, ok := readCPU()
	if !ok {
		return 0
	}
	cpuMu.Lock()
	defer cpuMu.Unlock()
	defer func() { lastIdle, lastTotal, haveLastCPU = idle, total, true }()
	if !haveLastCPU || total <= lastTotal {
		return 0
	}
	dt := float64(total - lastTotal)
	v := (dt - float64(idle-lastIdle)) / dt * 100
	if v < 0 {
		v = 0
	}
	return float64(int(v*10+0.5)) / 10
}

func memStatus() (memoryStatusEx, bool) {
	var ms memoryStatusEx
	ms.Length = uint32(unsafe.Sizeof(ms))
	r, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&ms)))
	return ms, r != 0
}

func collectPlatform(m *Metrics) {
	m.CPUPercent = cpuPercent()
	if ms, ok := memStatus(); ok {
		m.MemTotal = ms.TotalPhys
		m.MemUsed = ms.TotalPhys - ms.AvailPhys
		// Page file beyond physical memory, comparable to swap.
		if ms.TotalPageFile > ms.TotalPhys {
			m.SwapTotal = ms.TotalPageFile - ms.TotalPhys
			used := (ms.TotalPageFile - ms.AvailPageFile)
			if used > m.MemUsed {
				m.SwapUsed = min(used-m.MemUsed, m.SwapTotal)
			}
		}
	}
	m.Disks = drives()
	sys := systemDrive()
	for _, d := range m.Disks {
		if strings.EqualFold(d.Path, sys) || m.DiskPath == "" {
			m.DiskPath, m.DiskTotal, m.DiskUsed = d.Path, d.Total, d.Used
		}
	}
	if r, _, _ := procGetTickCount64.Call(); r != 0 {
		m.UptimeSec = uint64(r) / 1000
	}
}

func systemDrive() string {
	d, err := windows.GetWindowsDirectory()
	if err != nil || len(d) < 2 {
		return `C:\`
	}
	return strings.ToUpper(d[:2]) + `\`
}

// drives lists the fixed drives (C:\, D:\, ...) with size and usage.
func drives() []Disk {
	buf := make([]uint16, 256)
	n, err := windows.GetLogicalDriveStrings(uint32(len(buf)), &buf[0])
	if err != nil || n == 0 {
		return nil
	}
	out := []Disk{}
	start := 0
	for i := 0; i < int(n); i++ {
		if buf[i] != 0 {
			continue
		}
		root := windows.UTF16ToString(buf[start:i])
		start = i + 1
		if root == "" {
			continue
		}
		p, _ := windows.UTF16PtrFromString(root)
		if windows.GetDriveType(p) != windows.DRIVE_FIXED {
			continue
		}
		var free, total, totalFree uint64
		if windows.GetDiskFreeSpaceEx(p, &free, &total, &totalFree) != nil || total == 0 {
			continue
		}
		fs := make([]uint16, 64)
		fsName := ""
		if windows.GetVolumeInformation(p, nil, 0, nil, nil, nil, &fs[0], uint32(len(fs))) == nil {
			fsName = windows.UTF16ToString(fs)
		}
		out = append(out, Disk{Path: root, FS: strings.ToLower(fsName), Total: total, Used: total - totalFree})
	}
	return out
}

func regString(k registry.Key, name string) string {
	s, _, _ := k.GetStringValue(name)
	return s
}

// Info returns static facts about the host: Windows edition, version, build and CPU.
func Info() map[string]any {
	info := map[string]any{"os_id": "windows"}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows NT\CurrentVersion`, registry.QUERY_VALUE); err == nil {
		product := regString(k, "ProductName")
		display := regString(k, "DisplayVersion")
		if display == "" {
			display = regString(k, "ReleaseId")
		}
		build := regString(k, "CurrentBuild")
		ubr, _, _ := k.GetIntegerValue("UBR")
		edition := regString(k, "EditionID")
		installType := regString(k, "InstallationType") // Client | Server | Server Core
		k.Close()
		// Windows 11 still says "Windows 10" in ProductName.
		if b, _ := strconv.Atoi(build); b >= 22000 && strings.Contains(product, "Windows 10") && installType == "Client" {
			product = strings.Replace(product, "Windows 10", "Windows 11", 1)
		}
		pretty := product
		if display != "" {
			pretty += " " + display
		}
		info["os_pretty_name"] = pretty
		info["os_version"] = display
		info["os_build"] = build + "." + strconv.FormatUint(ubr, 10)
		info["kernel"] = "NT " + build + "." + strconv.FormatUint(ubr, 10)
		info["os_edition"] = edition
		info["os_install_type"] = installType
	}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\CentralProcessor\0`, registry.QUERY_VALUE); err == nil {
		info["cpu_model"] = strings.TrimSpace(regString(k, "ProcessorNameString"))
		k.Close()
	}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\BIOS`, registry.QUERY_VALUE); err == nil {
		info["product"] = regString(k, "SystemProductName")
		info["vendor"] = regString(k, "SystemManufacturer")
		k.Close()
	}
	if ms, ok := memStatus(); ok {
		info["mem_total"] = ms.TotalPhys
	}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SYSTEM\CurrentControlSet\Services\Tcpip\Parameters`, registry.QUERY_VALUE); err == nil {
		if d := regString(k, "Domain"); d != "" {
			info["domain"] = d
		}
		k.Close()
	}
	return info
}
