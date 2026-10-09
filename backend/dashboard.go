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
	EM_SETSEL      = 0x00B1
	EM_REPLACESEL  = 0x00C2
	EM_SCROLLCARET = 0x00B7
	WM_VSCROLL     = 0x0115
	SB_BOTTOM      = 7

	MB_OK            = 0x00000000
	MB_ICONERROR     = 0x00000010
	MB_SETFOREGROUND = 0x00010000

	// CREATE_NO_WINDOW — prevents a console window for the child process
	CREATE_NO_WINDOW = 0x08000000
)

func sendMessage(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procSendMessageW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

func appendToEdit(edit *winc.MultiEdit, text string) {
	if text == "" {
		return
	}
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

// getExeDir returns the directory where the .exe is located.
// In `go run` mode, os.Executable() returns a path in the temp folder —
// so we detect that and fall back to os.Getwd().
func getExeDir() string {
	exePath, err := os.Executable()
	if err != nil {
		wd, _ := os.Getwd()
		return wd
	}

	exeDir := filepath.Dir(exePath)

	// Detect `go run` mode — .exe path is in Go's temp folder
	// Example: C:\Users\...\AppData\Local\Temp\go-build1234\b001\exe\main.exe
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
// contains .venv AND backend/ — starts from startDir, walks up to parent
// until found or until the filesystem root.
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
			// Reached filesystem root, stop
			return "", false
		}
		dir = parent
	}
}

// resolvePythonAndWorkDir returns (pythonExe, workDir).
// If not found, returns a clear error message.
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

	// Locate python.exe in the venv
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

// WidgetMap — references to the 4 MultiEdit widgets
type WidgetMap struct {
	Init     *winc.MultiEdit
	Server   *winc.MultiEdit
	Pipeline *winc.MultiEdit
	Ingest   *winc.MultiEdit
}

var widgets WidgetMap

// AppendLog — route log to the appropriate widget based on channel
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

	// Prevent a console window from appearing for the child process.
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
// GUI
// =============================================================================

func main() {
	// ── Resolve python & workDir from the .exe location ─────────────
	pythonExe, workDir, err := resolvePythonAndWorkDir()
	if err != nil {
		showFatalError(err.Error())
		return
	}

	// Debug info to stdout, in case something goes wrong
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

	// Save references to global widgets
	widgets.Init = editInit
	widgets.Server = editServer
	widgets.Pipeline = editPipeline
	widgets.Ingest = editIngest

	// ── Uptime updater ──────────────────────────────────────────────
	startTime := time.Now()
	uptimeTicker := time.NewTicker(1 * time.Second)
	go func() {
		for range uptimeTicker.C {
			elapsed := time.Since(startTime)
			hh := int(elapsed.Hours())
			mm := int(elapsed.Minutes()) % 60
			ss := int(elapsed.Seconds()) % 60
			text := fmt.Sprintf("Uptime: %02d:%02d:%02d", hh, mm, ss)
			lblUptime.SetText(text)
		}
	}()

	// ── Start button ────────────────────────────────────────────────
	btnStart.OnClick().Bind(func(e *winc.Event) {
		cmdMutex.Lock()
		if currentCmd != nil {
			cmdMutex.Unlock()
			return
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
			cmdMutex.Unlock()
			return
		}
		currentCmd = cmd
		currentCancel = cancel
		cmdMutex.Unlock()

		btnStart.SetEnabled(false)
		btnStop.SetEnabled(true)

		// Monitor completion
		go func() {
			err := cmd.Wait()
			cmdMutex.Lock()
			currentCmd = nil
			if currentCancel != nil {
				currentCancel()
				currentCancel = nil
			}
			cmdMutex.Unlock()

			btnStart.SetEnabled(true)
			btnStop.SetEnabled(false)

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
		defer cmdMutex.Unlock()
		if currentCmd == nil || currentCmd.Process == nil {
			return
		}
		AppendLog(LogEvent{
			TS:      time.Now().Format("15:04:05"),
			Level:   "WARN",
			Channel: "init",
			Msg:     "Stopping server...",
		})
		_ = currentCmd.Process.Kill()
	})

	// ── Clear button ────────────────────────────────────────────────
	btnClear.OnClick().Bind(func(e *winc.Event) {
		editInit.SetText("")
		editServer.SetText("")
		editPipeline.SetText("")
		editIngest.SetText("")
	})

	// ── Window close — kill Python ──────────────────────────────────
	win.OnClose().Bind(func(e *winc.Event) {
		cmdMutex.Lock()
		if currentCmd != nil && currentCmd.Process != nil {
			_ = currentCmd.Process.Kill()
		}
		if currentCancel != nil {
			currentCancel()
		}
		cmdMutex.Unlock()
		uptimeTicker.Stop()
		winc.Exit()
	})

	// ── Show ────────────────────────────────────────────────────────
	win.Show()
	win.Center()
	winc.RunMainLoop()
}