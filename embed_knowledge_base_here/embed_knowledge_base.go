package main

import (
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
	"unsafe"

	"github.com/tadvi/winc"
)

// Win32 API setup for real-time log streaming & auto-scrolling
var (
	user32           = syscall.NewLazyDLL("user32.dll")
	procSendMessageW = user32.NewProc("SendMessageW")
)

const (
	WM_VSCROLL     = 0x0115
	EM_SETSEL      = 0x00B1
	EM_REPLACESEL  = 0x00C2
	EM_SCROLLCARET = 0x00B7
	SB_BOTTOM      = 7
)

func sendMessage(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procSendMessageW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

type PyEnv struct {
	PythonCmd string
}

func main() {
	mainWindow := winc.NewForm(nil)
	mainWindow.SetSize(820, 680)
	mainWindow.SetText("Knowledge Base & Vector Store Manager")

	workDir, _ := filepath.Abs(".")

	// Control Buttons Layout
	btnCheckEnv := winc.NewPushButton(mainWindow)
	btnCheckEnv.SetPos(20, 15)
	btnCheckEnv.SetSize(180, 32)
	btnCheckEnv.SetText("🔍 Check Environment")

	btnEmbed := winc.NewPushButton(mainWindow)
	btnEmbed.SetPos(210, 15)
	btnEmbed.SetSize(180, 32)
	btnEmbed.SetText("🚀 1. Embed Knowledge Base")

	btnDeleteKB := winc.NewPushButton(mainWindow)
	btnDeleteKB.SetPos(20, 55)
	btnDeleteKB.SetSize(180, 32)
	btnDeleteKB.SetText("🗑️ 2. Delete Knowledge Base")

	btnDeleteMemory := winc.NewPushButton(mainWindow)
	btnDeleteMemory.SetPos(210, 55)
	btnDeleteMemory.SetSize(180, 32)
	btnDeleteMemory.SetText("💬 3. Delete Chat Memory")

	btnClear := winc.NewPushButton(mainWindow)
	btnClear.SetPos(400, 55)
	btnClear.SetSize(180, 32)
	btnClear.SetText("🧹 Clear Log")

	// Stop Button
	btnStop := winc.NewPushButton(mainWindow)
	btnStop.SetPos(590, 15)
	btnStop.SetSize(190, 72)
	btnStop.SetText("🛑 STOP")
	btnStop.SetEnabled(false)

	// Console Log Area
	logEdit := winc.NewMultiEdit(mainWindow)
	logEdit.SetPos(20, 100)
	logEdit.SetSize(760, 520)

	// High-performance Win32 direct-append (O(1) complexity, avoids UI freezing)
	appendLog := func(msg string) {
		if msg == "" {
			return
		}
		text := msg + "\r\n"
		ptr, err := syscall.UTF16PtrFromString(text)
		if err != nil {
			return
		}

		// Move selection cursor to the end of the edit control
		sendMessage(logEdit.Handle(), EM_SETSEL, ^uintptr(0), ^uintptr(0))
		// Append text at the cursor position
		sendMessage(logEdit.Handle(), EM_REPLACESEL, 0, uintptr(unsafe.Pointer(ptr)))
		// Auto-scroll to caret
		sendMessage(logEdit.Handle(), EM_SCROLLCARET, 0, 0)
		sendMessage(logEdit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
	}

	allButtons := []*winc.PushButton{
		btnCheckEnv, btnEmbed, btnDeleteKB, btnDeleteMemory, btnClear,
	}

	setButtonsEnabled := func(enabled bool) {
		for _, btn := range allButtons {
			btn.SetEnabled(enabled)
		}
		btnStop.SetEnabled(!enabled)
	}

	var cancelTask context.CancelFunc

	runAsync := func(actionName string, taskFn func(ctx context.Context) error) {
		setButtonsEnabled(false)

		ctx, cancel := context.WithCancel(context.Background())
		cancelTask = cancel

		appendLog(strings.Repeat("=", 70))
		appendLog(fmt.Sprintf("▶️  Running: %s...", actionName))
		appendLog(strings.Repeat("=", 70))

		go func() {
			defer setButtonsEnabled(true)

			err := taskFn(ctx)
			if ctx.Err() != nil {
				appendLog("\r\n[!] Task stopped by user.")
			} else if err != nil {
				appendLog(fmt.Sprintf("\r\n❌ ERROR: %v", err))
			} else {
				appendLog(fmt.Sprintf("\r\n✅ %s completed successfully.", actionName))
			}
		}()
	}

	btnCheckEnv.OnClick().Bind(func(e *winc.Event) {
		runAsync("Check Environment", func(ctx context.Context) error {
			return checkEnvironment(ctx, workDir, appendLog)
		})
	})

	btnEmbed.OnClick().Bind(func(e *winc.Event) {
		runAsync("Embed Knowledge Base", func(ctx context.Context) error {
			env := resolvePythonEnv()
			return runCommand(ctx, appendLog, workDir, env.PythonCmd, "embedder.py")
		})
	})

	btnDeleteKB.OnClick().Bind(func(e *winc.Event) {
		runAsync("Delete Knowledge Base", func(ctx context.Context) error {
			env := resolvePythonEnv()
			return runCommand(ctx, appendLog, workDir, env.PythonCmd, "delete_knowledge_base.py")
		})
	})

	btnDeleteMemory.OnClick().Bind(func(e *winc.Event) {
		runAsync("Delete Chat Memory", func(ctx context.Context) error {
			env := resolvePythonEnv()
			return runCommand(ctx, appendLog, workDir, env.PythonCmd, "delete_chat_memory.py")
		})
	})

	btnClear.OnClick().Bind(func(e *winc.Event) {
		logEdit.SetText("")
	})

	btnStop.OnClick().Bind(func(e *winc.Event) {
		if cancelTask != nil {
			appendLog("\r\n[!] Cancelling process...")
			cancelTask()
		}
	})

	mainWindow.OnClose().Bind(func(e *winc.Event) {
		if cancelTask != nil {
			cancelTask()
		}
		winc.Exit()
	})

	mainWindow.Show()
	mainWindow.Center()
	winc.RunMainLoop()
}

// ==================== PYTHON RESOLVER ====================

func resolvePythonEnv() PyEnv {
	venvDirs := []string{
		filepath.Join("..", ".venv"),       // root/.venv
		filepath.Join("..", "..", ".venv"), // parent/.venv
		".venv",                            // local .venv
	}

	for _, venv := range venvDirs {
		pyPath := filepath.Join(venv, "Scripts", "python.exe")
		if !fileExists(pyPath) {
			pyPath = filepath.Join(venv, "bin", "python")
		}

		if fileExists(pyPath) {
			absPy, _ := filepath.Abs(pyPath)
			return PyEnv{PythonCmd: absPy}
		}
	}

	if absPy, err := exec.LookPath("python"); err == nil {
		return PyEnv{PythonCmd: absPy}
	}

	if runtime.GOOS == "windows" {
		return PyEnv{PythonCmd: "python"}
	}
	return PyEnv{PythonCmd: "python3"}
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func checkEnvironment(ctx context.Context, workDir string, logFn func(string)) error {
	env := resolvePythonEnv()
	logFn(fmt.Sprintf("[check] Working dir: %s", workDir))
	logFn(fmt.Sprintf("[check] Python binary: %s", env.PythonCmd))

	datasetPath := filepath.Join(workDir, "..", "dataset")
	absDataset, _ := filepath.Abs(datasetPath)
	logFn(fmt.Sprintf("[check] Dataset folder: %s", absDataset))

	if fileExists(absDataset) {
		logFn("  ✅ Dataset directory found.")
	} else {
		logFn("  ❌ Dataset directory NOT found!")
	}

	checks := []struct {
		name string
		args []string
	}{
		{"Python Version", []string{"--version"}},
		{"ChromaDB", []string{"-c", "import chromadb; print('ChromaDB OK')"}},
		{"Neo4j Driver", []string{"-c", "import neo4j; print('Neo4j Driver OK')"}},
		{"pdfplumber", []string{"-c", "import pdfplumber; print('pdfplumber OK')"}},
	}

	for _, c := range checks {
		logFn(fmt.Sprintf("\n[check] Checking %s...", c.name))
		if err := runCommand(ctx, logFn, workDir, env.PythonCmd, c.args...); err != nil {
			logFn(fmt.Sprintf("  ❌ %s unavailable", c.name))
		}
	}

	return nil
}

// ==================== STREAMING EXEC COMMAND ====================

// Real-time byte-level pipe reader (flushes progress indicators like \r immediately)
func streamPipe(r io.Reader, logFn func(string)) {
	buf := make([]byte, 512)
	var lineBuf strings.Builder

	for {
		n, err := r.Read(buf)
		if n > 0 {
			chunk := string(buf[:n])
			for _, ch := range chunk {
				if ch == '\n' || ch == '\r' {
					line := strings.TrimSpace(lineBuf.String())
					if line != "" {
						logFn(line)
					}
					lineBuf.Reset()
				} else {
					lineBuf.WriteRune(ch)
				}
			}
		}
		if err != nil {
			break
		}
	}

	if lineBuf.Len() > 0 {
		line := strings.TrimSpace(lineBuf.String())
		if line != "" {
			logFn(line)
		}
	}
}

func runCommand(ctx context.Context, logFn func(string), workDir, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	if workDir != "" {
		cmd.Dir = workDir
	}

	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: 0x08000000, // CREATE_NO_WINDOW
	}

	// Disable Python buffer so prints & progress bars emit instantly
	cmd.Env = append(os.Environ(),
		"PYTHONUNBUFFERED=1",
		"PYTHONIOENCODING=utf-8",
	)

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

	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		streamPipe(stdout, logFn)
	}()

	go func() {
		defer wg.Done()
		streamPipe(stderr, logFn)
	}()

	wg.Wait()
	return cmd.Wait()
}