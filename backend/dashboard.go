package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/tadvi/winc"
)

// =============================================================================
// WIN32 API — Append to MultiEdit + MessageBoxW
// =============================================================================

var (
	user32           = syscall.NewLazyDLL("user32.dll")
	procSendMessageW = user32.NewProc("SendMessageW")
	procMessageBoxW  = user32.NewProc("MessageBoxW")
)

const (
	EM_SETSEL       = 0x00B1
	EM_REPLACESEL   = 0x00C2
	EM_SCROLLCARET  = 0x00B7
	EM_GETLINECOUNT = 0x00BA
	EM_LINEINDEX    = 0x00BB
	WM_VSCROLL      = 0x0115
	SB_BOTTOM       = 7

	MB_OK            = 0x00000000
	MB_ICONERROR     = 0x00000010
	MB_SETFOREGROUND = 0x00010000

	// CREATE_NO_WINDOW — prevents a console window for the child process
	CREATE_NO_WINDOW = 0x08000000

	// Batas maksimum line per panel log. Kalau lewat, line paling lama
	// dihapus dari atas supaya tidak kena limit internal EDIT control.
	maxLinesPerEdit = 5000
)

// emptyUTF16 — pointer ke UTF-16 NUL tunggal, dipakai untuk EM_REPLACESEL
// saat menghapus selection (string kosong biasa tidak selalu bekerja).
var emptyUTF16 = []uint16{0}

func sendMessage(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procSendMessageW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

func getLineCount(edit *winc.MultiEdit) int {
	return int(sendMessage(edit.Handle(), EM_GETLINECOUNT, 0, 0))
}

// trimOldLines menghapus line paling atas sampai tersisa <= maxLines-1,
// supaya ada slot untuk line baru yang akan di-append.
func trimOldLines(edit *winc.MultiEdit, maxLines int) {
	count := getLineCount(edit)
	if count <= maxLines {
		return
	}

	// Hapus (count - maxLines + 1) line dari atas
	linesToRemove := count - maxLines + 1

	// EM_LINEINDEX(line) → character index awal line tsb.
	// Kalau baris tidak ada, return -1.
	lineIdx := int(sendMessage(edit.Handle(), EM_LINEINDEX, uintptr(linesToRemove), 0))
	if lineIdx <= 0 {
		// fallback: hapus semua
		edit.SetText("")
		return
	}

	// Select [0, lineIdx) — ini tepat menghapus linesToRemove line
	// beserta CRLF-nya (EM_LINEINDEX menunjuk ke char pertama line berikutnya).
	sendMessage(edit.Handle(), EM_SETSEL, 0, uintptr(lineIdx))
	// EM_REPLACESEL dengan UTF16 NUL tunggal → hapus selection
	sendMessage(edit.Handle(), EM_REPLACESEL, 0, uintptr(unsafe.Pointer(&emptyUTF16[0])))
}

func appendToEdit(edit *winc.MultiEdit, text string) {
	if text == "" {
		return
	}

	// Trim SEBELUM append supaya tidak pernah kena limit internal EDIT control.
	trimOldLines(edit, maxLinesPerEdit)

	line := text + "\r\n"
	ptr, err := syscall.UTF16PtrFromString(line)
	if err != nil {
		return
	}
	sendMessage(edit.Handle(), EM_SETSEL, ^uintptr(0), ^uintptr(0))
	sendMessage(edit.Handle(), EM_REPLACESEL, 0, uintptr(unsafe.Pointer(ptr)))
	sendMessage(edit.Handle(), EM_SCROLLCARET, 0, 0)
	sendMessage(edit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
}

func showFatalError(msg string) {
	titlePtr, _ := syscall.UTF16PtrFromString("RAGNA Dashboard — Error")
	msgPtr, _ := syscall.UTF16PtrFromString(msg)

	procMessageBoxW.Call(
		0,
		uintptr(unsafe.Pointer(msgPtr)),
		uintptr(unsafe.Pointer(titlePtr)),
		uintptr(MB_OK|MB_ICONERROR|MB_SETFOREGROUND),
	)
}

// =============================================================================
// PATH RESOLVER — Find project root, venv, and workDir portably
// =============================================================================

func getExeDir() string {
	exePath, err := os.Executable()
	if err != nil {
		wd, _ := os.Getwd()
		return wd
	}

	exeDir := filepath.Dir(exePath)

	// Detect `go run` mode — .exe path is in Go's temp folder
	lowerDir := strings.ToLower(exeDir)
	if strings.Contains(lowerDir, "go-build") ||
		(strings.Contains(lowerDir, "temp") && strings.Contains(lowerDir, "exe")) {
		wd, _ := os.Getwd()
		return wd
	}

	return exeDir
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

// findProjectRoot finds the project root by looking for a folder that
// contains .venv AND backend/ — starts from startDir, walks up to parent.
func findProjectRoot(startDir string) (string, bool) {
	dir, err := filepath.Abs(startDir)
	if err != nil {
		return "", false
	}

	for {
		venvWin := filepath.Join(dir, ".venv", "Scripts", "python.exe")
		venvUnix := filepath.Join(dir, ".venv", "bin", "python")
		backendDir := filepath.Join(dir, "backend")

		venvExists := fileExists(venvWin) || fileExists(venvUnix)
		backendExists := dirExists(backendDir)

		if venvExists && backendExists {
			return dir, true
		}

		parent := filepath.Dir(dir)
		if parent == dir {
			return "", false
		}
		dir = parent
	}
}

// resolvePythonAndWorkDir returns (pythonExe, workDir).
func resolvePythonAndWorkDir() (string, string, error) {
	startDir := getExeDir()

	projectRoot, found := findProjectRoot(startDir)
	if !found {
		return "", "", fmt.Errorf(
			"Cannot find the project root.\n\n"+
				"The project root must contain:\n"+
				"  - .venv/Scripts/python.exe (or .venv/bin/python)\n"+
				"  - backend/main.py\n\n"+
				"Start dir: %s\n\n"+
				"Make sure the .venv and backend/ folders are somewhere "+
				"above the .exe in the directory tree.",
			startDir)
	}

	var pythonExe string
	if runtime.GOOS == "windows" {
		pythonExe = filepath.Join(projectRoot, ".venv", "Scripts", "python.exe")
	} else {
		pythonExe = filepath.Join(projectRoot, ".venv", "bin", "python")
	}

	if !fileExists(pythonExe) {
		return "", "", fmt.Errorf(
			"python.exe not found in the venv:\n  %s\n\n"+
				"Make sure the venv is created and dependencies are installed.",
			pythonExe)
	}

	workDir := filepath.Join(projectRoot, "backend")
	if !dirExists(workDir) {
		return "", "", fmt.Errorf(
			"backend/ folder not found:\n  %s", workDir)
	}

	mainPy := filepath.Join(workDir, "main.py")
	if !fileExists(mainPy) {
		return "", "", fmt.Errorf(
			"main.py not found in backend/:\n  %s", mainPy)
	}

	return pythonExe, workDir, nil
}

// =============================================================================
// WINDOWS JOB OBJECT — jaminan kill-on-close untuk seluruh process tree
//
// Dengan Job Object + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, saat handle job
// ditutup (baik normal maupun karena proses launcher mati), OS otomatis
// membunuh SEMUA proses yang ter-assign — tanpa bergantung pada taskkill.
// =============================================================================

var (
	kernel32                     = syscall.NewLazyDLL("kernel32.dll")
	procCreateJobObjectW         = kernel32.NewProc("CreateJobObjectW")
	procAssignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	procSetInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	procCloseHandle              = kernel32.NewProc("CloseHandle")
)

// JOBOBJECT_EXTENDED_LIMIT_INFORMATION layout (x64).
// Kita hanya butuh field BasicLimitInformation.LimitFlags (offset 16 dari awal).
// Struct penuh: JOBOBJECT_BASIC_LIMIT_INFORMATION (64 bytes) +
// IO_COUNTERS (48 bytes) + SIZE_T x2 (16 bytes) = 128 bytes.
type jobObjectExtendedLimitInformation struct {
	BasicLimitInformation struct {
		PerProcessUserTimeLimit int64
		PerJobUserTimeLimit     int64
		LimitFlags              uint32
		MinimumWorkingSetSize   uintptr
		MaximumWorkingSetSize   uintptr
		ActiveProcessLimit      uint32
		Affinity                uintptr
		PriorityClass           uint32
		SchedulingClass         uint32
	}
	IoInfo struct {
		ReadOperationCount  uint64
		WriteOperationCount uint64
		OtherOperationCount uint64
		ReadTransferCount   uint64
		WriteTransferCount  uint64
		OtherTransferCount  uint64
	}
	ProcessMemoryLimit  uintptr
	JobMemoryLimit      uintptr
	PeakProcessMemoryUsed uintptr
	PeakJobMemoryUsed     uintptr
}

const (
	JobObjectExtendedLimitInformationClass = 9
	JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE     = 0x00002000
)

var (
	jobHandle     syscall.Handle
	jobHandleOnce sync.Once
	jobHandleErr  error
)

// ensureJobObject membuat (sekali) job object dengan KILL_ON_JOB_CLOSE.
func ensureJobObject() (syscall.Handle, error) {
	jobHandleOnce.Do(func() {
		h, _, err := procCreateJobObjectW.Call(0, 0)
		if h == 0 {
			jobHandleErr = fmt.Errorf("CreateJobObjectW failed: %v", err)
			return
		}
		jobHandle = syscall.Handle(h)

		var info jobObjectExtendedLimitInformation
		info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

		ret, _, err := procSetInformationJobObject.Call(
			uintptr(jobHandle),
			uintptr(JobObjectExtendedLimitInformationClass),
			uintptr(unsafe.Pointer(&info)),
			unsafe.Sizeof(info),
		)
		if ret == 0 {
			jobHandleErr = fmt.Errorf("SetInformationJobObject failed: %v", err)
			procCloseHandle.Call(uintptr(jobHandle))
			jobHandle = 0
		}
	})
	return jobHandle, jobHandleErr
}

// assignToJobObject menambahkan proses ke job object global.
func assignToJobObject(pid int) error {
	if runtime.GOOS != "windows" {
		return nil
	}
	h, err := ensureJobObject()
	if err != nil {
		return err
	}
	// PROCESS_SET_QUOTA (0x0100) | PROCESS_TERMINATE (0x0001)
	procHandle, err := syscall.OpenProcess(0x0100|0x0001, false, uint32(pid))
	if err != nil {
		return err
	}
	defer syscall.CloseHandle(procHandle)

	ret, _, callErr := procAssignProcessToJobObject.Call(
		uintptr(h), uintptr(procHandle),
	)
	if ret == 0 {
		return fmt.Errorf("AssignProcessToJobObject failed: %v", callErr)
	}
	return nil
}

// killJobObject menutup job handle → OS kill semua proses di dalamnya.
// Setelah ini, job object tidak bisa dipakai lagi; panggil ulang
// ensureJobObject akan membuat instance baru (jobHandleOnce tidak reset,
// jadi untuk pemakaian ulang kita expose resetJobObject).
func killJobObject() {
	if runtime.GOOS != "windows" {
		return
	}
	if jobHandle != 0 {
		procCloseHandle.Call(uintptr(jobHandle))
		jobHandle = 0
	}
	// Reset once supaya start berikutnya membuat job baru
	jobHandleOnce = sync.Once{}
}

// =============================================================================
// PROCESS TREE KILLER
// =============================================================================

// processAlive cek apakah PID masih hidup.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	if runtime.GOOS == "windows" {
		cmd := exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/NH")
		cmd.SysProcAttr = &syscall.SysProcAttr{
			HideWindow:    true,
			CreationFlags: CREATE_NO_WINDOW,
		}
		out, err := cmd.Output()
		if err != nil {
			return false
		}
		s := strings.ToLower(string(out))
		if strings.Contains(s, "no tasks") {
			return false
		}
		return strings.Contains(s, strconv.Itoa(pid))
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return proc.Signal(syscall.Signal(0)) == nil
}

// killProcessTree membunuh seluruh process tree (parent + children).
//
// Windows: taskkill /F /T /PID <pid> + verifikasi + retry sekali.
// Unix: SIGTERM, tunggu, SIGKILL kalau masih hidup.
func killProcessTree(pid int) {
	if pid <= 0 {
		return
	}
	if runtime.GOOS == "windows" {
		for attempt := 0; attempt < 2; attempt++ {
			killCmd := exec.Command(
				"taskkill", "/F", "/T", "/PID", strconv.Itoa(pid),
			)
			killCmd.SysProcAttr = &syscall.SysProcAttr{
				HideWindow:    true,
				CreationFlags: CREATE_NO_WINDOW,
			}
			_ = killCmd.Run()
			if !processAlive(pid) {
				return
			}
			time.Sleep(150 * time.Millisecond)
		}
	} else {
		if proc, err := os.FindProcess(pid); err == nil {
			_ = proc.Signal(syscall.SIGTERM)
			time.Sleep(300 * time.Millisecond)
			if processAlive(pid) {
				_ = proc.Signal(syscall.SIGKILL)
			}
		}
	}
}

// =============================================================================
// ORPHAN PROCESS DETECTION
// =============================================================================

// findOrphanPythonProcesses mencari python.exe yang masih jalan dengan
// argumen main.py — indikasi proses server yang belum mati.
//
// Return: slice of PID (kosong kalau tidak ada orphan).
//
// Strategi: coba wmic dulu (cepat), fallback ke PowerShell (lambat tapi akurat).
func findOrphanPythonProcesses() []int {
	if runtime.GOOS != "windows" {
		return nil
	}

	// Coba wmic dulu
	pids := findViaWmic()
	if pids != nil {
		return pids
	}

	// Fallback ke PowerShell (mis. Windows 11 yang sudah tidak ada wmic)
	return findViaPowerShell()
}

// findViaWmic query WMI via wmic untuk list python.exe + command line-nya.
// Return nil kalau wmic gagal / tidak ada (biar fallback ke PowerShell).
func findViaWmic() []int {
	cmd := exec.Command(
		"wmic", "process", "where", "name='python.exe'",
		"get", "ProcessId,CommandLine", "/format:csv",
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: CREATE_NO_WINDOW,
	}

	out, err := cmd.Output()
	if err != nil {
		return nil
	}
	return parseWmicCSV(string(out))
}

// findViaPowerShell fallback — pakai Get-CimInstance.
func findViaPowerShell() []int {
	psScript := `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ` +
		`Where-Object { ` +
		`$_.CommandLine -like '*main.py*' -or ` +
		`$_.CommandLine -like '*multiprocessing.spawn*' -or ` +
		`$_.CommandLine -like '*multiprocessing.resource_tracker*' -or ` +
		`$_.CommandLine -like '*uvicorn*' -or ` +
		`$_.CommandLine -like '*watchfiles*' -or ` +
		// Idle multiprocessing child: command line hanya path python.exe
		// (tidak ada argumen setelah .exe)
		`($_.CommandLine -match 'pythonw?\.exe"?\s*$') ` +
		`} | ` +
		`Select-Object -ExpandProperty ProcessId`

	cmd := exec.Command(
		"powershell", "-NoProfile", "-NonInteractive", "-Command", psScript,
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: CREATE_NO_WINDOW,
	}

	out, err := cmd.Output()
	if err != nil {
		return nil
	}

	var pids []int
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if pid, err := strconv.Atoi(line); err == nil {
			pids = append(pids, pid)
		}
	}
	return pids
}

// parseWmicCSV parse output wmic CSV format.
//
// Format:
//   Node,CommandLine,ProcessId
//   HOST,"C:\path\python.exe main.py",12345
//   HOST,"C:\path\python.exe -c ...",23456
func parseWmicCSV(out string) []int {
	var pids []int
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "Node,") {
			continue
		}

		lower := strings.ToLower(line)

		// Signature 1: server parent / child
		matched := strings.Contains(lower, "main.py") ||
			strings.Contains(lower, "multiprocessing.spawn") ||
			strings.Contains(lower, "multiprocessing.resource_tracker") ||
			strings.Contains(lower, "uvicorn") ||
			strings.Contains(lower, "watchfiles")

		// Signature 2: idle multiprocessing child (command line hanya path)
		// Format CSV: HOST,"C:\path\python.exe",PID
		// Cek apakah setelah "python.exe" ada argumen atau tidak.
		if !matched {
			// Extract command line (kolom ke-2 CSV, dalam quote)
			parts := strings.SplitN(line, ",", 3)
			if len(parts) >= 2 {
				cmdline := strings.Trim(parts[1], `"`)
				cmdlineLower := strings.ToLower(cmdline)
				// Kalau command line berakhir dengan "python.exe" (tanpa argumen)
				if strings.HasSuffix(cmdlineLower, "python.exe") ||
					strings.HasSuffix(cmdlineLower, "pythonw.exe") {
					matched = true
				}
			}
		}

		if !matched {
			continue
		}

		// Extract PID dari kolom terakhir
		parts := strings.Split(line, ",")
		if len(parts) < 3 {
			continue
		}
		pidStr := strings.TrimSpace(parts[len(parts)-1])
		if pid, err := strconv.Atoi(pidStr); err == nil {
			pids = append(pids, pid)
		}
	}
	return pids
}

// =============================================================================
// DATA
// =============================================================================

type LogEvent struct {
	TS      string `json:"ts"`
	Level   string `json:"level"`
	Logger  string `json:"logger"`
	Channel string `json:"channel"`
	Msg     string `json:"msg"`
}

type StateEvent struct {
	Phase      string  `json:"phase"`
	Step       string  `json:"step,omitempty"`
	Label      string  `json:"label,omitempty"`
	Status     string  `json:"status"`
	Elapsed    float64 `json:"elapsed,omitempty"`
	Message    string  `json:"message,omitempty"`
	TotalSteps int     `json:"total_steps,omitempty"`
}

type WidgetMap struct {
	Init     *winc.MultiEdit
	Server   *winc.MultiEdit
	Pipeline *winc.MultiEdit
	Ingest   *winc.MultiEdit
}

var widgets WidgetMap

func AppendLog(ev LogEvent) {
	line := fmt.Sprintf("[%s] %-4s  %s", ev.TS, ev.Level, ev.Msg)

	var target *winc.MultiEdit
	switch ev.Channel {
	case "init":
		target = widgets.Init
	case "server":
		target = widgets.Server
	case "pipeline":
		target = widgets.Pipeline
	case "ingest":
		target = widgets.Ingest
	default:
		target = widgets.Server
	}
	if target != nil {
		appendToEdit(target, line)
	}
}

// =============================================================================
// SUBPROCESS
// =============================================================================

var (
	currentCmd    *exec.Cmd
	currentCancel context.CancelFunc
	cmdMutex      sync.Mutex
)

func startPython(workDir, pythonExe string) (*exec.Cmd, context.CancelFunc, error) {
	ctx, cancel := context.WithCancel(context.Background())

	cmd := exec.CommandContext(ctx, pythonExe, "main.py")
	cmd.Dir = workDir
	cmd.Env = append(os.Environ(),
		"RAGNA_PROGRESS=1",
		"RAGNA_DASHBOARD=0",
		"PYTHONUNBUFFERED=1",
		"PYTHONIOENCODING=utf-8",
	)

	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: CREATE_NO_WINDOW,
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return nil, nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		cancel()
		return nil, nil, err
	}

	if err := cmd.Start(); err != nil {
		cancel()
		return nil, nil, err
	}

	// Assign ke Job Object supaya seluruh tree otomatis dibunuh saat
	// launcher mati / job handle ditutup.
	if runtime.GOOS == "windows" {
		if err := assignToJobObject(cmd.Process.Pid); err != nil {
			// Non-fatal: kita masih punya taskkill + orphan scan sebagai backup.
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "WARN",
				Channel: "init",
				Msg:     "Job Object assign failed (using taskkill fallback): " + err.Error(),
			})
		}
	}

	go readStream(stdout, false)
	go readStream(stderr, true)

	return cmd, cancel, nil
}

func readStream(r io.Reader, isStderr bool) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if isStderr {
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "RAW",
				Channel: "server",
				Msg:     "[stderr] " + line,
			})
			continue
		}

		switch {
		case strings.HasPrefix(line, "@@RAGNA_STATE@@ "):
			handleState(line)
		case strings.HasPrefix(line, "@@RAGNA_LOG@@ "):
			handleLog(line)
		default:
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "RAW",
				Channel: "server",
				Msg:     line,
			})
		}
	}
}

func handleState(line string) {
	jsonStr := strings.TrimPrefix(line, "@@RAGNA_STATE@@ ")
	var ev StateEvent
	if err := json.Unmarshal([]byte(jsonStr), &ev); err != nil {
		return
	}

	switch ev.Phase {
	case "init":
		if ev.Step != "" {
			switch ev.Status {
			case "start":
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "INFO",
					Channel: "init",
					Msg:     fmt.Sprintf("⟳ %s...", ev.Label),
				})
			case "done":
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "INFO",
					Channel: "init",
					Msg:     fmt.Sprintf("✓ %s (%.2fs)", ev.Label, ev.Elapsed),
				})
			case "error":
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "ERROR",
					Channel: "init",
					Msg:     fmt.Sprintf("✗ %s — %s", ev.Label, ev.Message),
				})
			}
		}
		if ev.Status == "fatal" {
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "CRIT",
				Channel: "init",
				Msg:     "FATAL: " + ev.Message,
			})
		}

	case "ready":
		AppendLog(LogEvent{
			TS:      time.Now().Format("15:04:05"),
			Level:   "INFO",
			Channel: "init",
			Msg:     "✓ SERVER READY",
		})

	case "ingest":
		if ev.Step != "" {
			switch ev.Status {
			case "start":
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "INFO",
					Channel: "ingest",
					Msg:     fmt.Sprintf("⟳ %s...", ev.Label),
				})
			case "done":
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "INFO",
					Channel: "ingest",
					Msg:     fmt.Sprintf("✓ %s (%.2fs)", ev.Label, ev.Elapsed),
				})
			case "error":
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "ERROR",
					Channel: "ingest",
					Msg:     fmt.Sprintf("✗ %s — %s", ev.Label, ev.Message),
				})
			}
		}
	}
}

func handleLog(line string) {
	jsonStr := strings.TrimPrefix(line, "@@RAGNA_LOG@@ ")
	var ev LogEvent
	if err := json.Unmarshal([]byte(jsonStr), &ev); err != nil {
		return
	}
	AppendLog(ev)
}

// =============================================================================
// STOP HELPERS — dipakai Stop button & window close
// =============================================================================

// stopAllProcesses membunuh semua target + rescan + retry sampai bersih
// (atau maxRounds habis). Blocking; panggil dari goroutine kalau tidak mau
// freeze UI.
func stopAllProcesses(
	targets map[int]struct{},
	cmd *exec.Cmd,
	cancel context.CancelFunc,
	orphanPIDsPtr *[]int,
	orphanMutex *sync.Mutex,
	maxRounds int,
) bool {
	// Cancel context dulu — biar exec.CommandContext ikut terminate
	if cancel != nil {
		cancel()
	}

	for round := 0; round < maxRounds; round++ {
		// Kill semua target saat ini
		for pid := range targets {
			killProcessTree(pid)
		}

		// Tunggu sebentar
		time.Sleep(300 * time.Millisecond)

		// Rescan orphan — tangkap child baru / yang lolos
		pids := findOrphanPythonProcesses()

		orphanMutex.Lock()
		*orphanPIDsPtr = pids
		orphanMutex.Unlock()

		// Tambah PID baru ke target
		newFound := false
		for _, p := range pids {
			if _, ok := targets[p]; !ok {
				targets[p] = struct{}{}
				newFound = true
			}
		}

		// Cek apakah semua target sudah mati
		allDead := true
		for pid := range targets {
			if processAlive(pid) {
				allDead = false
				break
			}
		}

		if allDead && !newFound && len(pids) == 0 {
			return true
		}
	}
	return false
}

// =============================================================================
// GUI
// =============================================================================

func main() {
	// ── Resolve python & workDir from the .exe location ─────────────
	pythonExe, workDir, err := resolvePythonAndWorkDir()
	if err != nil {
		showFatalError(err.Error())
		return
	}

	fmt.Printf("[launcher] python  = %s\n", pythonExe)
	fmt.Printf("[launcher] workDir = %s\n", workDir)

	// ── Create the main window ──────────────────────────────────────
	win := winc.NewForm(nil)
	win.SetText("RAGNA Server Dashboard")
	win.SetSize(1020, 720)
	win.SetMinSize(800, 600)

	// ── Control buttons ─────────────────────────────────────────────
	btnStart := winc.NewPushButton(win)
	btnStart.SetText("▶ Start")
	btnStart.SetPos(10, 10)
	btnStart.SetSize(120, 32)

	btnStop := winc.NewPushButton(win)
	btnStop.SetText("■ Stop")
	btnStop.SetPos(140, 10)
	btnStop.SetSize(120, 32)
	btnStop.SetEnabled(false)

	btnClear := winc.NewPushButton(win)
	btnClear.SetText("🗑 Clear All")
	btnClear.SetPos(270, 10)
	btnClear.SetSize(120, 32)

	lblUptime := winc.NewLabel(win)
	lblUptime.SetText("Uptime: 00:00:00")
	lblUptime.SetPos(800, 15)
	lblUptime.SetSize(200, 20)

	// ── Label + MultiEdit for each panel ────────────────────────────
	lblInit := winc.NewLabel(win)
	lblInit.SetText("⚙️  INIT")
	lblInit.SetPos(10, 50)
	lblInit.SetSize(200, 20)

	editInit := winc.NewMultiEdit(win)
	editInit.SetPos(10, 75)
	editInit.SetSize(480, 180)

	lblServer := winc.NewLabel(win)
	lblServer.SetText("🖥️  SERVER")
	lblServer.SetPos(510, 50)
	lblServer.SetSize(200, 20)

	editServer := winc.NewMultiEdit(win)
	editServer.SetPos(510, 75)
	editServer.SetSize(480, 180)

	lblPipeline := winc.NewLabel(win)
	lblPipeline.SetText("🧠  PIPELINE")
	lblPipeline.SetPos(10, 270)
	lblPipeline.SetSize(200, 20)

	editPipeline := winc.NewMultiEdit(win)
	editPipeline.SetPos(10, 295)
	editPipeline.SetSize(980, 170)

	lblIngest := winc.NewLabel(win)
	lblIngest.SetText("📥  INGEST")
	lblIngest.SetPos(10, 480)
	lblIngest.SetSize(200, 20)

	editIngest := winc.NewMultiEdit(win)
	editIngest.SetPos(10, 505)
	editIngest.SetSize(980, 150)

	widgets.Init = editInit
	widgets.Server = editServer
	widgets.Pipeline = editPipeline
	widgets.Ingest = editIngest

	// ── Uptime state ────────────────────────────────────────────────
	var (
		uptimeStart   time.Time
		uptimeFrozen  time.Duration
		uptimeRunning bool
		uptimeMutex   sync.Mutex
	)

	// ── Orphan process state ────────────────────────────────────────
	var (
		orphanPIDs  []int
		orphanMutex sync.Mutex
	)
	scanTicker := time.NewTicker(3 * time.Second)

	// ── updateUptimeLabel ───────────────────────────────────────────
	updateUptimeLabel := func() {
		orphanMutex.Lock()
		orphanCount := len(orphanPIDs)
		orphanMutex.Unlock()

		cmdMutex.Lock()
		hasCurrent := currentCmd != nil
		cmdMutex.Unlock()

		if orphanCount > 0 && !hasCurrent {
			lblUptime.SetText(fmt.Sprintf("⚠ %d orphan process", orphanCount))
			return
		}

		uptimeMutex.Lock()
		var d time.Duration
		if uptimeRunning {
			d = time.Since(uptimeStart)
		} else {
			d = uptimeFrozen
		}
		uptimeMutex.Unlock()

		hh := int(d.Hours())
		mm := int(d.Minutes()) % 60
		ss := int(d.Seconds()) % 60
		lblUptime.SetText(fmt.Sprintf("Uptime: %02d:%02d:%02d", hh, mm, ss))
	}

	// ── updateStopButtonState ───────────────────────────────────────
	updateStopButtonState := func() {
		cmdMutex.Lock()
		hasCurrent := currentCmd != nil
		cmdMutex.Unlock()

		orphanMutex.Lock()
		hasOrphan := len(orphanPIDs) > 0
		orphanMutex.Unlock()

		btnStop.SetEnabled(hasCurrent || hasOrphan)
	}

	// Ticker: update label uptime tiap detik
	uptimeTicker := time.NewTicker(1 * time.Second)
	go func() {
		for range uptimeTicker.C {
			updateUptimeLabel()
		}
	}()

	// Ticker: scan orphan tiap 3 detik
	go func() {
		for range scanTicker.C {
			pids := findOrphanPythonProcesses()

			orphanMutex.Lock()
			orphanPIDs = pids
			orphanMutex.Unlock()

			updateStopButtonState()
			updateUptimeLabel()
		}
	}()

	// Scan sekali saat startup (delay 500ms biar window muncul dulu)
	go func() {
		time.Sleep(500 * time.Millisecond)
		pids := findOrphanPythonProcesses()

		orphanMutex.Lock()
		orphanPIDs = pids
		orphanMutex.Unlock()

		if len(pids) > 0 {
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "WARN",
				Channel: "init",
				Msg:     fmt.Sprintf("⚠ Found %d orphan process(es) — click Stop to clean up", len(pids)),
			})
		}

		updateStopButtonState()
		updateUptimeLabel()
	}()

	// ── Start button ────────────────────────────────────────────────
	btnStart.OnClick().Bind(func(e *winc.Event) {
		cmdMutex.Lock()
		if currentCmd != nil {
			cmdMutex.Unlock()
			return
		}
		cmdMutex.Unlock()

		// Cek orphan dulu — kalau ada, cleanup sebelum start
		orphanMutex.Lock()
		orphans := append([]int{}, orphanPIDs...)
		orphanMutex.Unlock()

		if len(orphans) > 0 {
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "WARN",
				Channel: "init",
				Msg:     fmt.Sprintf("Cleaning %d orphan process(es) before start...", len(orphans)),
			})
			for _, pid := range orphans {
				killProcessTree(pid)
			}
			time.Sleep(700 * time.Millisecond)

			pids := findOrphanPythonProcesses()
			orphanMutex.Lock()
			orphanPIDs = pids
			orphanMutex.Unlock()
		}

		AppendLog(LogEvent{
			TS:      time.Now().Format("15:04:05"),
			Level:   "INFO",
			Channel: "init",
			Msg:     "Starting server...",
		})

		cmd, cancel, err := startPython(workDir, pythonExe)
		if err != nil {
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "ERROR",
				Channel: "init",
				Msg:     "Failed to start Python: " + err.Error(),
			})
			return
		}

		cmdMutex.Lock()
		currentCmd = cmd
		currentCancel = cancel
		cmdMutex.Unlock()

		uptimeMutex.Lock()
		uptimeStart = time.Now()
		uptimeFrozen = 0
		uptimeRunning = true
		uptimeMutex.Unlock()
		updateUptimeLabel()

		btnStart.SetEnabled(false)
		updateStopButtonState()

		// Monitor completion
		go func() {
			err := cmd.Wait()

			uptimeMutex.Lock()
			if uptimeRunning {
				uptimeFrozen = time.Since(uptimeStart)
				uptimeRunning = false
			}
			uptimeMutex.Unlock()

			cmdMutex.Lock()
			currentCmd = nil
			if currentCancel != nil {
				currentCancel()
				currentCancel = nil
			}
			cmdMutex.Unlock()

			// Scan orphan sekali lagi (karena bisa jadi ada child yang
			// masih hidup setelah parent mati)
			time.Sleep(300 * time.Millisecond)
			pids := findOrphanPythonProcesses()
			orphanMutex.Lock()
			orphanPIDs = pids
			orphanMutex.Unlock()

			btnStart.SetEnabled(true)
			updateStopButtonState()
			updateUptimeLabel()

			msg := "Server stopped normally."
			level := "INFO"
			if err != nil {
				msg = "Server stopped with error: " + err.Error()
				level = "ERROR"
			}
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   level,
				Channel: "init",
				Msg:     msg,
			})
		}()
	})

	// ── Stop button ─────────────────────────────────────────────────
	btnStop.OnClick().Bind(func(e *winc.Event) {
		cmdMutex.Lock()
		cmd := currentCmd
		cancel := currentCancel
		cmdMutex.Unlock()

		// Freeze uptime dulu
		uptimeMutex.Lock()
		if uptimeRunning {
			uptimeFrozen = time.Since(uptimeStart)
			uptimeRunning = false
		}
		uptimeMutex.Unlock()

		// Kumpulkan semua PID target: current + orphan
		targets := map[int]struct{}{}
		if cmd != nil && cmd.Process != nil {
			targets[cmd.Process.Pid] = struct{}{}
		}
		orphanMutex.Lock()
		for _, pid := range orphanPIDs {
			targets[pid] = struct{}{}
		}
		orphanMutex.Unlock()

		if len(targets) == 0 {
			AppendLog(LogEvent{
				TS:      time.Now().Format("15:04:05"),
				Level:   "INFO",
				Channel: "init",
				Msg:     "No process to stop.",
			})
			updateUptimeLabel()
			return
		}

		AppendLog(LogEvent{
			TS:      time.Now().Format("15:04:05"),
			Level:   "WARN",
			Channel: "init",
			Msg:     fmt.Sprintf("Stopping %d process(es) (full tree)...", len(targets)),
		})

		// Disable Stop sebentar supaya tidak double-click
		btnStop.SetEnabled(false)

		// Kill + retry loop di background (jangan block UI)
		go func() {
			ok := stopAllProcesses(targets, cmd, cancel, &orphanPIDs, &orphanMutex, 10)
			if ok {
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "INFO",
					Channel: "init",
					Msg:     "✓ All processes stopped.",
				})
			} else {
				AppendLog(LogEvent{
					TS:      time.Now().Format("15:04:05"),
					Level:   "ERROR",
					Channel: "init",
					Msg:     "⚠ Some processes still alive after max retries. Check Task Manager.",
				})
			}

			// Reset currentCmd/cancel — cmd.Wait() di goroutine monitor
			// akan menangani sisanya.
			cmdMutex.Lock()
			currentCancel = nil
			cmdMutex.Unlock()

			updateStopButtonState()
			updateUptimeLabel()
		}()
	})

	// ── Clear button ────────────────────────────────────────────────
	btnClear.OnClick().Bind(func(e *winc.Event) {
		editInit.SetText("")
		editServer.SetText("")
		editPipeline.SetText("")
		editIngest.SetText("")
	})

	// ── Window close — kill everything ──────────────────────────────
	win.OnClose().Bind(func(e *winc.Event) {
		cmdMutex.Lock()
		cmd := currentCmd
		cancel := currentCancel
		cmdMutex.Unlock()

		// Kumpulkan semua target
		targets := map[int]struct{}{}
		if cmd != nil && cmd.Process != nil {
			targets[cmd.Process.Pid] = struct{}{}
		}
		orphanMutex.Lock()
		for _, pid := range orphanPIDs {
			targets[pid] = struct{}{}
		}
		orphanMutex.Unlock()

		// Kill loop singkat — jangan biarkan window close sebelum bersih.
		// Pakai maxRounds lebih kecil supaya close tidak terlalu lama.
		if len(targets) > 0 {
			stopAllProcesses(targets, cmd, cancel, &orphanPIDs, &orphanMutex, 8)
		}

		// Tutup Job Object → OS kill semua child yang tersisa.
		killJobObject()

		scanTicker.Stop()
		uptimeTicker.Stop()
		winc.Exit()
	})

	// ── Show ────────────────────────────────────────────────────────
	win.Show()
	win.Center()
	winc.RunMainLoop()
}