package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/tadvi/winc"
)

// ============================================================================
// Win32 API setup (log auto-scroll + job object for process-tree kill)
// ============================================================================

var (
	user32           = syscall.NewLazyDLL("user32.dll")
	procSendMessageW = user32.NewProc("SendMessageW")

	kernel32                     = syscall.NewLazyDLL("kernel32.dll")
	procCreateJobObjectW         = kernel32.NewProc("CreateJobObjectW")
	procSetInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	procAssignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	procTerminateJobObject       = kernel32.NewProc("TerminateJobObject")
)

const (
	WM_VSCROLL     = 0x0115
	EM_SETSEL      = 0x00B1
	EM_SCROLLCARET = 0x00B7
	EM_REPLACESEL  = 0x00C2
	SB_BOTTOM      = 7

	// Batas maksimum baris yang disimpan di log
	maxLogLines = 5000
	// Berapa baris yang dipangkas sekaligus saat batas tercapai
	// (biar tidak trim tiap baris -> lebih efisien)
	trimChunkSize = 500
)

const (
	JobObjectExtendedLimitInformation  = 9
	JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
)

type JOBOBJECT_BASIC_LIMIT_INFORMATION struct {
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

type IO_COUNTERS struct {
	ReadOperationCount  uint64
	WriteOperationCount uint64
	OtherOperationCount uint64
	ReadTransferCount   uint64
	WriteTransferCount  uint64
	OtherTransferCount  uint64
}

type JOBOBJECT_EXTENDED_LIMIT_INFORMATION struct {
	BasicLimitInformation JOBOBJECT_BASIC_LIMIT_INFORMATION
	IoInfo                IO_COUNTERS
	ProcessMemoryLimit    uintptr
	JobMemoryLimit        uintptr
	PeakProcessMemoryUsed uintptr
	PeakJobMemoryUsed     uintptr
}

func sendMessage(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procSendMessageW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

func utf16Ptr(s string) *uint16 {
	p, _ := syscall.UTF16PtrFromString(s)
	return p
}

// ============================================================================
// Types
// ============================================================================

type PyTorchOption struct {
	Label string
	Args  []string
}

type GPUInfo struct {
	Name        string
	CudaVersion float64
	IsNvidia    bool
	IsAMD       bool
}

// ============================================================================
// Log buffer (dibatasi maxLogLines, trim otomatis)
//
// PENTING: semua method di sini HARUS dipanggil dari UI thread saja.
// Tidak ada locking tambahan di sini karena hanya UI thread yang menyentuh.
// ============================================================================

type logBuffer struct {
	lines []string
	edit  *winc.MultiEdit
}

func newLogBuffer(edit *winc.MultiEdit) *logBuffer {
	return &logBuffer{
		edit:  edit,
		lines: make([]string, 0, maxLogLines+trimChunkSize),
	}
}

// reset mengosongkan buffer dan kontrol EDIT. UI thread only.
func (lb *logBuffer) reset() {
	lb.lines = lb.lines[:0]
	lb.edit.SetText("")
}

// append menambahkan pesan ke buffer + kontrol EDIT, dengan trim otomatis.
// UI thread only.
func (lb *logBuffer) append(msg string) {
	// Pecah pesan multiline menjadi baris terpisah
	for _, line := range strings.Split(msg, "\n") {
		line = strings.TrimRight(line, "\r")
		lb.lines = append(lb.lines, line)
	}

	// Jika melebihi batas, pangkas baris paling lama
	trimmed := 0
	if len(lb.lines) > maxLogLines {
		trimmed = len(lb.lines) - maxLogLines
		// Pangkas sedikit lebih banyak untuk menghindari trim tiap baris
		trimmed += trimChunkSize
		if trimmed > len(lb.lines) {
			trimmed = len(lb.lines)
		}
		// Pertahankan slice capacity agar tidak realokasi terus-menerus
		copy(lb.lines, lb.lines[trimmed:])
		lb.lines = lb.lines[:len(lb.lines)-trimmed]
	}

	// Sinkronkan ke kontrol EDIT
	if trimmed > 0 {
		lb.rebuildEdit()
	} else {
		lb.appendToEdit(msg)
	}
}

// appendToEdit menambahkan teks ke akhir kontrol tanpa baca-ulang seluruh isi.
func (lb *logBuffer) appendToEdit(msg string) {
	hwnd := lb.edit.Handle()
	// Letakkan caret di akhir: gunakan nilai besar, Win32 akan clamp ke akhir teks.
	sendMessage(hwnd, EM_SETSEL, 0x7FFFFFFF, 0x7FFFFFFF)
	sendMessage(hwnd, EM_REPLACESEL, 0, uintptr(unsafe.Pointer(utf16Ptr(msg+"\r\n"))))
	lb.scrollToBottom()
}

// rebuildEdit menulis ulang seluruh buffer (dipanggil hanya saat trim).
func (lb *logBuffer) rebuildEdit() {
	lb.edit.SetText(strings.Join(lb.lines, "\r\n"))
	lb.scrollToBottom()
}

func (lb *logBuffer) scrollToBottom() {
	hwnd := lb.edit.Handle()
	sendMessage(hwnd, EM_SETSEL, 0x7FFFFFFF, 0x7FFFFFFF)
	sendMessage(hwnd, EM_SCROLLCARET, 0, 0)
	sendMessage(hwnd, WM_VSCROLL, SB_BOTTOM, 0)
}

// ============================================================================
// UI message queue (diproses dari UI thread via time.Ticker)
// ============================================================================

type uiMessage struct {
	kind string // "log", "install_start", "install_done", "clear_log"
	text string
}

var uiQueue = make(chan uiMessage, 4000)

func postLog(msg string) {
	select {
	case uiQueue <- uiMessage{kind: "log", text: msg}:
	default:
		// queue penuh -> buang; lebih baik kehilangan log daripada freeze
	}
}

// ============================================================================
// Main
// ============================================================================

func main() {
	mainWindow := winc.NewForm(nil)
	mainWindow.SetSize(660, 600)
	mainWindow.SetText("Auto Requirements & PyTorch Installer")

	currentOS := runtime.GOOS
	gpuInfo := detectGPU(currentOS)

	// Detected Info Labels
	osLabel := winc.NewLabel(mainWindow)
	osLabel.SetPos(20, 10)
	osLabel.SetSize(600, 20)
	osLabel.SetText(fmt.Sprintf("Auto-Detected System OS: %s", strings.Title(currentOS)))

	gpuLabel := winc.NewLabel(mainWindow)
	gpuLabel.SetPos(20, 30)
	gpuLabel.SetSize(600, 20)
	gpuLabel.SetText(fmt.Sprintf("Auto-Detected Hardware: %s", gpuInfo.Name))

	// OS Selector Dropdown
	osSelectLabel := winc.NewLabel(mainWindow)
	osSelectLabel.SetPos(20, 65)
	osSelectLabel.SetSize(120, 20)
	osSelectLabel.SetText("Target OS:")

	osCombo := winc.NewComboBox(mainWindow)
	osCombo.SetPos(150, 60)
	osCombo.SetSize(460, 150)

	osNames := []string{"Windows", "Linux", "macOS"}
	osTypeMap := map[string]string{
		"Windows": "windows",
		"Linux":   "linux",
		"macOS":   "darwin",
	}

	defaultOSIdx := 0
	switch currentOS {
	case "windows":
		defaultOSIdx = 0
	case "linux":
		defaultOSIdx = 1
	case "darwin":
		defaultOSIdx = 2
	}

	for i, name := range osNames {
		osCombo.InsertItem(i, name)
	}
	osCombo.SetSelectedItem(defaultOSIdx)

	// PyTorch Version Dropdown
	ptLabel := winc.NewLabel(mainWindow)
	ptLabel.SetPos(20, 100)
	ptLabel.SetSize(120, 20)
	ptLabel.SetText("PyTorch Version:")

	pytorchCombo := winc.NewComboBox(mainWindow)
	pytorchCombo.SetPos(150, 95)
	pytorchCombo.SetSize(460, 150)

	// Dynamic PyTorch state tracking
	var optionsMap map[string]PyTorchOption
	var currentOptionLabels []string
	var defaultOptionKey string

	updatePyTorchCombo := func(osType string) {
		for i := len(currentOptionLabels) - 1; i >= 0; i-- {
			pytorchCombo.DeleteItem(i)
		}

		optionsMap, currentOptionLabels, defaultOptionKey = getPyTorchOptions(osType, gpuInfo)

		defaultIdx := 0
		for i, label := range currentOptionLabels {
			pytorchCombo.InsertItem(i, label)
			if label == defaultOptionKey {
				defaultIdx = i
			}
		}
		pytorchCombo.SetSelectedItem(defaultIdx)
	}

	updatePyTorchCombo(currentOS)

	osCombo.OnSelectedChange().Bind(func(e *winc.Event) {
		selectedOSName := osCombo.Text()
		selectedOSType, ok := osTypeMap[selectedOSName]
		if ok {
			updatePyTorchCombo(selectedOSType)
		}
	})

	// Requirements file entry
	reqLabel := winc.NewLabel(mainWindow)
	reqLabel.SetPos(20, 135)
	reqLabel.SetSize(120, 20)
	reqLabel.SetText("Requirements File:")

	reqFileInput := winc.NewEdit(mainWindow)
	reqFileInput.SetPos(150, 130)
	reqFileInput.SetSize(460, 22)
	defaultReqPath := filepath.Join("requirements.txt")
	reqFileInput.SetText(defaultReqPath)

	// Install Button
	installBtn := winc.NewPushButton(mainWindow)
	installBtn.SetPos(20, 165)
	installBtn.SetSize(440, 32)
	installBtn.SetText("Install Requirements")

	// Stop Button
	stopBtn := winc.NewPushButton(mainWindow)
	stopBtn.SetPos(470, 165)
	stopBtn.SetSize(140, 32)
	stopBtn.SetText("Stop")
	stopBtn.SetEnabled(false)

	// Console Log Area
	logEdit := winc.NewMultiEdit(mainWindow)
	logEdit.SetPos(20, 210)
	logEdit.SetSize(590, 330)

	// Buffer log dengan batas 5000 baris + auto-scroll
	logBuf := newLogBuffer(logEdit)

	// appendLog: kirim ke queue UI, aman dari goroutine mana pun.
	appendLog := func(msg string) {
		postLog(msg)
	}

	var cancelInstall context.CancelFunc

	installBtn.OnClick().Bind(func(e *winc.Event) {
		installBtn.SetEnabled(false)
		stopBtn.SetEnabled(true)
		osCombo.SetEnabled(false)
		pytorchCombo.SetEnabled(false)
		reqFileInput.SetEnabled(false)

		logBuf.reset()

		ctx, cancel := context.WithCancel(context.Background())
		cancelInstall = cancel

		// Snapshot nilai dari UI thread SEBELUM masuk goroutine (winc tidak thread-safe)
		reqFile := reqFileInput.Text()
		selectedOptionLabel := pytorchCombo.Text()
		torchOpt, exists := optionsMap[selectedOptionLabel]

		go func() {
			defer func() {
				// Beri tahu UI thread bahwa install selesai
				uiQueue <- uiMessage{kind: "install_done"}
				cancelInstall = nil
			}()

			if !exists {
				appendLog("[-] Error: Invalid PyTorch option selected.")
				return
			}

			venvPythonPath, err := ensureVenv(ctx, appendLog)
			if err != nil {
				if ctx.Err() != nil {
					appendLog("\r\n[!] Installation stopped by user.")
				} else {
					appendLog(fmt.Sprintf("[-] Error setting up .venv: %v", err))
				}
				return
			}

			appendLog("\r\n[>] Upgrading pip in root .venv...")
			if err := runCommand(ctx, appendLog, venvPythonPath, "-m", "pip", "install", "--upgrade", "pip"); err != nil {
				if ctx.Err() != nil {
					appendLog("\r\n[!] Installation stopped by user.")
					return
				}
				appendLog(fmt.Sprintf("[-] Warning: pip upgrade failed: %v", err))
			}

			appendLog(fmt.Sprintf("\r\n[>] Installing PyTorch (%s)...", selectedOptionLabel))
			pipArgs := append([]string{"-m", "pip", "install"}, torchOpt.Args...)
			if err := runCommand(ctx, appendLog, venvPythonPath, pipArgs...); err != nil {
				if ctx.Err() != nil {
					appendLog("\r\n[!] Installation stopped by user.")
				} else {
					appendLog(fmt.Sprintf("[-] Failed to install PyTorch: %v", err))
				}
				return
			}

			if _, err := os.Stat(reqFile); os.IsNotExist(err) {
				appendLog(fmt.Sprintf("\r\n[-] Warning: File '%s' not found. Skipping requirements install.", reqFile))
			} else {
				appendLog(fmt.Sprintf("\r\n[>] Installing requirements from '%s'...", reqFile))
				if err := runCommand(ctx, appendLog, venvPythonPath, "-m", "pip", "install", "-r", reqFile); err != nil {
					if ctx.Err() != nil {
						appendLog("\r\n[!] Installation stopped by user.")
					} else {
						appendLog(fmt.Sprintf("[-] Failed to install requirements: %v", err))
					}
					return
				}
			}

			appendLog("\r\n[OK] ALL INSTALLATIONS COMPLETED SUCCESSFULLY!")
		}()
	})

	stopBtn.OnClick().Bind(func(e *winc.Event) {
		if cancelInstall != nil {
			appendLog("\r\n[!] Stopping installation process (killing process tree)...")
			cancelInstall()
		}
	})

	mainWindow.OnClose().Bind(func(e *winc.Event) {
		if cancelInstall != nil {
			cancelInstall()
		}
		winc.Exit()
	})

	// ------------------------------------------------------------------
	// UI pump: proses queue pesan dari goroutine di UI thread.
	// Dijalankan sebagai goroutine, tapi SETIAP akses winc di sini
	// terjadi dari goroutine ini -- dan RunMainLoop berjalan di goroutine
	// utama. Ini masih bukan UI thread yang sama...
	//
	// Solusi aman: jalankan pump lewat timer yang dijalankan oleh
	// message loop winc? Sayangnya winc tidak mengekspos timer.
	//
	// Praktik yang dipakai luas dengan tadvi/winc: panggil method kontrol
	// dari goroutine lain memang "tidak resmi" tapi dalam praktik stabil
	// karena winc menggunakan SendMessage (blocking) untuk SetText dll,
	// yang otomatis marshals ke thread pembuat window.
	//
	// Kita pakai itu: pump berjalan di goroutine sendiri, tapi setiap
	// SetText/SetEnabled dijamin di-marshal oleh USER32 via SendMessage.
	// ------------------------------------------------------------------
	go func() {
		ticker := time.NewTicker(50 * time.Millisecond)
		defer ticker.Stop()
		for range ticker.C {
			// Ambil semua pesan yang tersedia
			for {
				select {
				case m := <-uiQueue:
					switch m.kind {
					case "log":
						logBuf.append(m.text)
					case "install_done":
						installBtn.SetEnabled(true)
						stopBtn.SetEnabled(false)
						osCombo.SetEnabled(true)
						pytorchCombo.SetEnabled(true)
						reqFileInput.SetEnabled(true)
					}
				default:
					goto done
				}
			}
		done:
		}
	}()

	mainWindow.Show()
	mainWindow.Center()
	winc.RunMainLoop()
}

// ============================================================================
// GPU detection
// ============================================================================

func detectGPU(osType string) GPUInfo {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	info := GPUInfo{Name: "CPU / Integrated Graphics"}

	cmd := exec.CommandContext(ctx, "nvidia-smi")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: 0x08000000,
	}

	out, err := cmd.Output()
	if err == nil {
		outStr := string(out)
		info.IsNvidia = true

		if idx := strings.Index(outStr, "CUDA Version:"); idx != -1 {
			sub := outStr[idx+len("CUDA Version:"):]
			sub = strings.TrimSpace(sub)
			fields := strings.Fields(sub)
			if len(fields) > 0 {
				fmt.Sscanf(fields[0], "%f", &info.CudaVersion)
			}
		}

		cmdName := exec.CommandContext(ctx, "nvidia-smi", "--query-gpu=name", "--format=csv,noheader")
		cmdName.SysProcAttr = &syscall.SysProcAttr{
			HideWindow:    true,
			CreationFlags: 0x08000000,
		}
		if outName, errName := cmdName.Output(); errName == nil && len(strings.TrimSpace(string(outName))) > 0 {
			gpuName := strings.TrimSpace(string(outName))
			if info.CudaVersion > 0 {
				info.Name = fmt.Sprintf("%s (CUDA %.1f)", gpuName, info.CudaVersion)
			} else {
				info.Name = gpuName
			}
		} else {
			info.Name = "NVIDIA GPU"
		}
		return info
	}

	if osType == "windows" {
		ctxWin, cancelWin := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancelWin()

		cmdWin := exec.CommandContext(ctxWin, "powershell", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name")
		cmdWin.SysProcAttr = &syscall.SysProcAttr{
			HideWindow:    true,
			CreationFlags: 0x08000000,
		}
		outWin, errWin := cmdWin.Output()
		if errWin == nil {
			lines := strings.Split(string(outWin), "\n")
			for _, line := range lines {
				line = strings.TrimSpace(line)
				upper := strings.ToUpper(line)
				if strings.Contains(upper, "NVIDIA") {
					info.IsNvidia = true
					info.Name = line
					return info
				} else if strings.Contains(upper, "AMD") || strings.Contains(upper, "RADEON") {
					info.IsAMD = true
					info.Name = line
					return info
				}
			}
		}
	}
	return info
}

// ============================================================================
// PyTorch options
// ============================================================================

func getPyTorchOptions(osType string, gpu GPUInfo) (map[string]PyTorchOption, []string, string) {
	options := make(map[string]PyTorchOption)
	var labels []string
	defaultKey := ""

	switch osType {
	case "windows":
		options["CUDA 13.2"] = PyTorchOption{
			Label: "CUDA 13.2",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu132"},
		}
		options["CUDA 13"] = PyTorchOption{
			Label: "CUDA 13",
			Args:  []string{"torch", "torchvision"},
		}
		options["CUDA 12.6"] = PyTorchOption{
			Label: "CUDA 12.6",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu126"},
		}
		options["CPU Only"] = PyTorchOption{
			Label: "CPU Only",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"},
		}

		labels = []string{"CUDA 13.2", "CUDA 13", "CUDA 12.6", "CPU Only"}

		if gpu.IsNvidia {
			if gpu.CudaVersion >= 13.2 || gpu.CudaVersion == 0 {
				defaultKey = "CUDA 13.2"
			} else if gpu.CudaVersion >= 13.0 {
				defaultKey = "CUDA 13"
			} else if gpu.CudaVersion >= 12.6 {
				defaultKey = "CUDA 12.6"
			} else {
				defaultKey = "CUDA 13.2"
			}
		} else {
			defaultKey = "CPU Only"
		}

	case "linux":
		options["CUDA 13.2"] = PyTorchOption{
			Label: "CUDA 13.2",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu132"},
		}
		options["CUDA 13"] = PyTorchOption{
			Label: "CUDA 13",
			Args:  []string{"torch", "torchvision"},
		}
		options["CUDA 12.6"] = PyTorchOption{
			Label: "CUDA 12.6",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu126"},
		}
		options["AMD ROCm 7.14"] = PyTorchOption{
			Label: "AMD ROCm 7.14",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/rocm7.14"},
		}
		options["CPU Only"] = PyTorchOption{
			Label: "CPU Only",
			Args:  []string{"torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"},
		}

		labels = []string{"CUDA 13.2", "CUDA 13", "CUDA 12.6", "AMD ROCm 7.14", "CPU Only"}

		if gpu.IsNvidia {
			if gpu.CudaVersion >= 13.2 || gpu.CudaVersion == 0 {
				defaultKey = "CUDA 13.2"
			} else if gpu.CudaVersion >= 13.0 {
				defaultKey = "CUDA 13"
			} else if gpu.CudaVersion >= 12.6 {
				defaultKey = "CUDA 12.6"
			} else {
				defaultKey = "CUDA 13.2"
			}
		} else if gpu.IsAMD {
			defaultKey = "AMD ROCm 7.14"
		} else {
			defaultKey = "CPU Only"
		}

	case "darwin":
		options["Default (MPS / Metal)"] = PyTorchOption{
			Label: "Default (MPS / Metal)",
			Args:  []string{"torch", "torchvision"},
		}
		labels = []string{"Default (MPS / Metal)"}
		defaultKey = "Default (MPS / Metal)"
	}

	return options, labels, defaultKey
}

// ============================================================================
// Venv creation + activation (with logging)
// ============================================================================

func ensureVenv(ctx context.Context, logFn func(string)) (string, error) {
	venvDir := filepath.Join("..", ".venv")
	var venvPython string

	if runtime.GOOS == "windows" {
		venvPython = filepath.Join(venvDir, "Scripts", "python.exe")
	} else {
		venvPython = filepath.Join(venvDir, "bin", "python")
	}

	absVenv, _ := filepath.Abs(venvDir)
	absPython, _ := filepath.Abs(venvPython)

	if _, err := os.Stat(venvDir); os.IsNotExist(err) {
		logFn("[>] .venv directory not found in root (..). Creating virtual environment...")
		logFn(fmt.Sprintf("    Target path: %s", absVenv))

		systemPython := "python"
		if runtime.GOOS != "windows" {
			systemPython = "python3"
		}

		logFn(fmt.Sprintf("[>] Running: %s -m venv %s", systemPython, absVenv))
		if err := runCommand(ctx, logFn, systemPython, "-m", "venv", venvDir); err != nil {
			return "", fmt.Errorf("failed to create virtualenv in parent dir: %w", err)
		}
		logFn("[OK] Virtual environment created successfully.")
	} else {
		logFn(fmt.Sprintf("[OK] Existing .venv found at: %s", absVenv))
	}

	// Verify venv python really exists
	if _, err := os.Stat(venvPython); os.IsNotExist(err) {
		return "", fmt.Errorf("venv python not found at %s (venv mungkin korup)", absPython)
	}

	// Explicit "activate" -- all subsequent commands use this interpreter
	logFn(fmt.Sprintf("[OK] Activating virtual environment: %s", absVenv))
	logFn(fmt.Sprintf("    Interpreter: %s", absPython))

	// Mark environment active for child processes (equivalent to `source activate`)
	os.Setenv("VIRTUAL_ENV", absVenv)
	os.Setenv("PYTHONUNBUFFERED", "1")
	venvBinDir := filepath.Dir(absPython)
	os.Setenv("PATH", venvBinDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	logFn("[OK] VIRTUAL_ENV dan PATH telah di-set. Semua proses selanjutnya berjalan di dalam venv.")

	return venvPython, nil
}

// ============================================================================
// Command runner with process-tree kill (Job Object on Windows)
// ============================================================================

func runCommand(ctx context.Context, logFn func(string), name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)

	// Hide command prompt window
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: 0x08000000, // CREATE_NO_WINDOW
	}

	// Unbuffer Python stdout so pip output streams immediately
	cmd.Env = append(os.Environ(), "PYTHONUNBUFFERED=1")

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return err
	}

	if err := cmd.Start(); err != nil {
		return err
	}

	// Create a Job Object that kills every process inside it on close.
	var jobHandle syscall.Handle
	if runtime.GOOS == "windows" {
		if h, jerr := createKillOnCloseJob(); jerr == nil && h != 0 {
			jobHandle = h
			// Assign the started process into the job.
			const PROCESS_SET_QUOTA = 0x0100
			const PROCESS_TERMINATE = 0x0001
			hProc, oerr := syscall.OpenProcess(PROCESS_SET_QUOTA|PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
			if oerr == nil {
				procAssignProcessToJobObject.Call(uintptr(jobHandle), uintptr(hProc))
				syscall.CloseHandle(hProc)
			} else {
				// If assignment fails, close the job and fall back to taskkill only
				syscall.CloseHandle(jobHandle)
				jobHandle = 0
			}
		}
	}

	// Watchdog: kill entire tree when context is cancelled.
	done := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			killProcessTree(cmd.Process.Pid, jobHandle, logFn)
		case <-done:
		}
	}()

	lines := make(chan string, 500)
	var wg sync.WaitGroup
	wg.Add(2)

	scanPipe := func(r io.Reader) {
		defer wg.Done()
		scanner := bufio.NewScanner(r)
		scanner.Split(func(data []byte, atEOF bool) (advance int, token []byte, err error) {
			if atEOF && len(data) == 0 {
				return 0, nil, nil
			}
			for i, b := range data {
				if b == '\n' || b == '\r' {
					return i + 1, data[:i], nil
				}
			}
			if atEOF {
				return len(data), data, nil
			}
			return 0, nil, nil
		})

		for scanner.Scan() {
			text := strings.TrimSpace(scanner.Text())
			if text != "" {
				lines <- text
			}
		}
	}

	go scanPipe(stdout)
	go scanPipe(stderr)

	go func() {
		wg.Wait()
		close(lines)
	}()

	for line := range lines {
		logFn(line)
	}

	err = cmd.Wait()
	close(done)

	// Clean up job object (kills any lingering child on close)
	if jobHandle != 0 {
		procTerminateJobObject.Call(uintptr(jobHandle), 1)
		syscall.CloseHandle(jobHandle)
	}

	return err
}

// createKillOnCloseJob creates a Windows Job Object that terminates all
// contained processes when the handle is closed.
func createKillOnCloseJob() (syscall.Handle, error) {
	h, _, e := procCreateJobObjectW.Call(0, 0)
	if h == 0 {
		return 0, e
	}

	var info JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

	r, _, e := procSetInformationJobObject.Call(
		h,
		uintptr(JobObjectExtendedLimitInformation),
		uintptr(unsafe.Pointer(&info)),
		unsafe.Sizeof(info),
	)
	if r == 0 {
		syscall.CloseHandle(syscall.Handle(h))
		return 0, e
	}
	return syscall.Handle(h), nil
}

// killProcessTree kills a process and all of its children.
func killProcessTree(pid int, job syscall.Handle, logFn func(string)) {
	if runtime.GOOS == "windows" {
		// Terminate via job object first (covers every child in the job)
		if job != 0 {
			procTerminateJobObject.Call(uintptr(job), 1)
		}
		// Fallback: taskkill /T to kill the whole tree even if job failed
		killCmd := exec.Command("taskkill", "/T", "/F", "/PID", fmt.Sprintf("%d", pid))
		killCmd.SysProcAttr = &syscall.SysProcAttr{
			HideWindow:    true,
			CreationFlags: 0x08000000,
		}
		_ = killCmd.Run()
		if logFn != nil {
			logFn("[!] Process tree terminated (job object + taskkill /T /F).")
		}
	} else {
		// Unix: kill the whole process group if possible
		if p, err := os.FindProcess(pid); err == nil {
			_ = p.Signal(syscall.SIGTERM)
			time.Sleep(300 * time.Millisecond)
			_ = p.Signal(syscall.SIGKILL)
			if logFn != nil {
				logFn("[!] Process tree terminated (SIGTERM -> SIGKILL).")
			}
		}
	}
}