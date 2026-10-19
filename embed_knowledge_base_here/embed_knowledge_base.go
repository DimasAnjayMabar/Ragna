//go:build windows

package main

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"github.com/tadvi/winc"
)

// ==================== WIN32 API ====================

var (
	user32                    = syscall.NewLazyDLL("user32.dll")
	procSendMessageW          = user32.NewProc("SendMessageW")
	procGetWindowTextW        = user32.NewProc("GetWindowTextW")
	procSystemParametersInfoW = user32.NewProc("SystemParametersInfoW")
)

const (
	WM_VSCROLL      = 0x0115
	EM_SETSEL       = 0x00B1
	EM_REPLACESEL   = 0x00C2
	EM_SCROLLCARET  = 0x00B7
	EM_SETLIMITTEXT = 0x00C5
	SB_BOTTOM       = 7
	SPI_GETWORKAREA = 0x0030
)

type RECT struct {
	Left   int32
	Top    int32
	Right  int32
	Bottom int32
}

func getScreenWorkArea() (width, height int) {
	var rect RECT
	procSystemParametersInfoW.Call(
		uintptr(SPI_GETWORKAREA),
		0,
		uintptr(unsafe.Pointer(&rect)),
		0,
	)
	w := int(rect.Right - rect.Left)
	h := int(rect.Bottom - rect.Top)
	if w <= 0 || h <= 0 {
		w = 1280
		h = 720
	}
	return w, h
}

func sendMessage(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procSendMessageW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

func getWindowText(hwnd uintptr) string {
	buf := make([]uint16, 1024)
	procGetWindowTextW.Call(hwnd, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	return syscall.UTF16ToString(buf)
}

type PyEnv struct {
	PythonCmd string
}

// ==================== CONSTANTS ====================

const (
	instructionFileName = "instruction_set.md"
	guardRailsFileName  = "guard_rails.md"
	maxLangButtons      = 6

	// [LOG-BUFFER] batas log
	maxLogLines = 5000
	maxLogChars = 200_000
)

var (
	instructionSlots = []string{
		"bot_name",
		"bot_identity",
		"social_guard_rail",
		"knowledge_guard_rail",
		"product_guard_rail",
		"memory_block_guard_rail",
		"memory_summary_block",
	}
	defaultInstructionLangs = []string{"id", "en"}
)

// ==================== [LOG-BUFFER] STATE & HELPERS ====================

var (
	logMu      sync.Mutex
	logLines   = make([]string, 0, maxLogLines) // ring buffer
	logPending = make([]string, 0, 256)         // antrian belum di-flush
)

// rebuildLogEdit menulis ulang seluruh isi kontrol dari ring buffer.
// WAJIB dipanggil dari UI thread.
func rebuildLogEdit(edit *winc.MultiEdit) {
	logMu.Lock()
	lines := make([]string, len(logLines))
	copy(lines, logLines)
	logMu.Unlock()

	text := strings.Join(lines, "\r\n")
	if len(text) > maxLogChars {
		cut := len(text) - maxLogChars
		text = text[cut:]
		if i := strings.Index(text, "\r\n"); i >= 0 {
			text = text[i+2:]
		}
	}

	edit.SetText(text)
	sendMessage(edit.Handle(), EM_SETSEL, ^uintptr(0), ^uintptr(0))
	sendMessage(edit.Handle(), EM_SCROLLCARET, 0, 0)
	sendMessage(edit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
}

// appendBatchToEdit menambahkan batch baris ke kontrol secara incremental.
// WAJIB dipanggil dari UI thread.
func appendBatchToEdit(edit *winc.MultiEdit, batch []string) {
	if len(batch) == 0 {
		return
	}
	text := strings.Join(batch, "\r\n") + "\r\n"

	curLen := len(edit.Text())
	if curLen+len(text) > maxLogChars {
		rebuildLogEdit(edit)
		return
	}

	ptr, err := syscall.UTF16PtrFromString(text)
	if err != nil {
		return
	}
	sendMessage(edit.Handle(), EM_SETSEL, ^uintptr(0), ^uintptr(0))
	sendMessage(edit.Handle(), EM_REPLACESEL, 0, uintptr(unsafe.Pointer(ptr)))
	sendMessage(edit.Handle(), EM_SCROLLCARET, 0, 0)
	sendMessage(edit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
}

// flushLogs memindahkan logPending ke ring buffer dan menampilkannya.
// WAJIB dipanggil dari UI thread.
func flushLogs(edit *winc.MultiEdit) {
	logMu.Lock()
	if len(logPending) == 0 {
		logMu.Unlock()
		return
	}
	batch := logPending
	logPending = nil
	logMu.Unlock()

	logMu.Lock()
	logLines = append(logLines, batch...)
	dropped := 0
	if len(logLines) > maxLogLines {
		dropped = len(logLines) - maxLogLines
		copy(logLines, logLines[dropped:])
		for i := maxLogLines; i < len(logLines); i++ {
			logLines[i] = ""
		}
		logLines = logLines[:maxLogLines]
	}
	logMu.Unlock()

	if dropped > 0 {
		rebuildLogEdit(edit)
		return
	}
	appendBatchToEdit(edit, batch)
}

// enqueueLog memasukkan pesan ke antrian (goroutine-safe).
func enqueueLog(msg string) {
	if msg == "" {
		return
	}
	normalized := strings.ReplaceAll(msg, "\r\n", "\n")
	for _, line := range strings.Split(normalized, "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" {
			continue
		}
		logMu.Lock()
		logPending = append(logPending, line)
		logMu.Unlock()
	}
}

// clearLogBuffers mengosongkan antrian + ring buffer.
func clearLogBuffers() {
	logMu.Lock()
	logPending = logPending[:0]
	logLines = logLines[:0]
	logMu.Unlock()
}

// ==================== NEO4J CHECK SCRIPT ====================

const neo4jCheckScript = `
import os, sys
from pathlib import Path

def load_dotenv():
    here = Path.cwd()
    for base in [here, here.parent, here.parent.parent]:
        envfile = base / ".env"
        if envfile.is_file():
            print("neo4j: loading %s" % envfile)
            for raw in envfile.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
            return envfile
    return None

found = load_dotenv()
if not found:
    print("neo4j FAIL: .env not found (cwd=%s)" % os.getcwd())
    sys.exit(1)

try:
    from neo4j import GraphDatabase
    uri  = os.environ.get("NEO4J_URI", "")
    user = os.environ.get("NEO4J_USER", "") or os.environ.get("NEO4J_USERNAME", "")
    pw   = os.environ.get("NEO4J_PASS", "") or os.environ.get("NEO4J_PASSWORD", "")
    if not uri or not user or not pw:
        print("neo4j FAIL: NEO4J_URI / NEO4J_USER / NEO4J_PASS not set")
        sys.exit(1)
    drv = GraphDatabase.driver(uri, auth=(user, pw), connection_timeout=3)
    drv.verify_connectivity()
    with drv.session() as s:
        s.run("RETURN 1").consume()
    drv.close()
    print("neo4j OK: %s" % uri)
except Exception as e:
    print("neo4j FAIL: %s" % e)
    sys.exit(1)
`

// ==================== EDITOR LAYOUT ====================

type EditorLayout struct {
	WindowWidth  int
	WindowHeight int

	TopBarY int
	TopBarH int

	LangLabelX int
	LangLabelW int
	LangLabelH int

	LangBtnStartX int
	LangBtnWidth  int
	LangBtnGap    int

	InputX int
	InputY int
	InputW int
	InputH int

	AddBtnX int
	AddBtnW int
	AddBtnH int

	RemoveBtnX int
	RemoveBtnW int
	RemoveBtnH int

	SlotStartY      int
	SlotLabelH      int
	SlotFieldHeight int
	SlotFieldGap    int
	SlotLabelX      int
	SlotLabelW      int
	SlotFieldX      int
	SlotFieldW      int

	BottomBtnY int
	BottomBtnH int
	SaveBtnX   int
	SaveBtnW   int
	EmbedBtnX  int
	EmbedBtnW  int
	CloseBtnX  int
	CloseBtnW  int
}

func computeEditorLayout(winW, winH int) EditorLayout {
	const (
		marginX      = 10
		marginTop    = 10
		marginBottom = 10
		labelHeight  = 20
		minFieldH    = 30
	)

	topBarY := marginTop
	topBarH := 32

	langLabelX := marginX
	langLabelW := 70
	langLabelH := 22

	langBtnStartX := langLabelX + langLabelW + 5
	langBtnWidth := 55
	langBtnGap := 4

	rightBlockW := 110 + 10 + 80 + 10 + 90
	inputX := winW - marginX - rightBlockW
	inputY := topBarY + 3
	inputW := 110
	inputH := 26

	addBtnX := inputX + inputW + 10
	addBtnW := 80
	addBtnH := 32

	removeBtnX := addBtnX + addBtnW + 10
	removeBtnW := 90
	removeBtnH := 32

	bottomBtnH := 32
	bottomBtnY := winH - marginBottom - bottomBtnH

	saveBtnX := marginX
	saveBtnW := 220

	embedBtnX := saveBtnX + saveBtnW + 10
	embedBtnW := 380

	closeBtnW := 130
	closeBtnX := winW - marginX - closeBtnW

	slotStartY := topBarY + topBarH + 25

	slotAreaBottom := bottomBtnY - 8
	availableHeight := slotAreaBottom - slotStartY

	numSlots := len(instructionSlots)
	slotFieldGap := 6

	slotFieldHeight := (availableHeight-slotFieldGap*(numSlots-1))/numSlots
	if slotFieldHeight < labelHeight+minFieldH {
		slotFieldHeight = labelHeight + minFieldH
	}

	slotLabelX := marginX
	slotLabelW := winW - 2*marginX
	if slotLabelW < 100 {
		slotLabelW = 100
	}
	slotFieldX := marginX
	slotFieldW := winW - 2*marginX
	if slotFieldW < 100 {
		slotFieldW = 100
	}

	return EditorLayout{
		WindowWidth:     winW,
		WindowHeight:    winH,
		TopBarY:         topBarY,
		TopBarH:         topBarH,
		LangLabelX:      langLabelX,
		LangLabelW:      langLabelW,
		LangLabelH:      langLabelH,
		LangBtnStartX:   langBtnStartX,
		LangBtnWidth:    langBtnWidth,
		LangBtnGap:      langBtnGap,
		InputX:          inputX,
		InputY:          inputY,
		InputW:          inputW,
		InputH:          inputH,
		AddBtnX:         addBtnX,
		AddBtnW:         addBtnW,
		AddBtnH:         addBtnH,
		RemoveBtnX:      removeBtnX,
		RemoveBtnW:      removeBtnW,
		RemoveBtnH:      removeBtnH,
		SlotStartY:      slotStartY,
		SlotLabelH:      labelHeight,
		SlotFieldHeight: slotFieldHeight,
		SlotFieldGap:    slotFieldGap,
		SlotLabelX:      slotLabelX,
		SlotLabelW:      slotLabelW,
		SlotFieldX:      slotFieldX,
		SlotFieldW:      slotFieldW,
		BottomBtnY:      bottomBtnY,
		BottomBtnH:      bottomBtnH,
		SaveBtnX:        saveBtnX,
		SaveBtnW:        saveBtnW,
		EmbedBtnX:       embedBtnX,
		EmbedBtnW:       embedBtnW,
		CloseBtnX:       closeBtnX,
		CloseBtnW:       closeBtnW,
	}
}

func main() {
	screenW, screenH := getScreenWorkArea()

	mainWindow := winc.NewForm(nil)
	mainWindow.SetSize(screenW, screenH)
	mainWindow.SetMinSize(1000, 480)
	mainWindow.SetText("Knowledge Base & Vector Store Manager")

	workDir, _ := filepath.Abs(".")

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

	btnRefreshLog := winc.NewPushButton(mainWindow)
	btnRefreshLog.SetPos(590, 55)
	btnRefreshLog.SetSize(180, 32)
	btnRefreshLog.SetText("🔄 Refresh Log")

	btnEditInstruction := winc.NewPushButton(mainWindow)
	btnEditInstruction.SetPos(400, 15)
	btnEditInstruction.SetSize(180, 32)
	btnEditInstruction.SetText("⚙️ Edit Instruction Set")

	btnEditGuardRails := winc.NewPushButton(mainWindow)
	btnEditGuardRails.SetPos(590, 15)
	btnEditGuardRails.SetSize(180, 32)
	btnEditGuardRails.SetText("🛡️ Edit Guard Rails")

	btnStop := winc.NewPushButton(mainWindow)
	btnStop.SetPos(780, 15)
	btnStop.SetSize(160, 72)
	btnStop.SetText("🛑 STOP")
	btnStop.SetEnabled(false)

	logEdit := winc.NewMultiEdit(mainWindow)
	logEdit.SetPos(20, 100)
	logEdit.SetSize(screenW-40, screenH-140)

	// [LOG-BUFFER] batasi panjang teks di kontrol Edit
	sendMessage(logEdit.Handle(), EM_SETLIMITTEXT, 0, maxLogChars)

	// [LOG-BUFFER] appendLog hanya enqueue; flush dilakukan manual di UI thread
	appendLog := func(msg string) {
		enqueueLog(msg)
	}

	allButtons := []*winc.PushButton{
		btnCheckEnv, btnEmbed, btnDeleteKB, btnDeleteMemory, btnClear,
		btnRefreshLog, btnEditInstruction, btnEditGuardRails,
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
		// [LOG-BUFFER] reset buffer + tampilan
		clearLogBuffers()
		logEdit.SetText("")
	})

	// [LOG-BUFFER] flush manual — tombol ini berjalan di UI thread
	btnRefreshLog.OnClick().Bind(func(e *winc.Event) {
		flushLogs(logEdit)
	})

	btnEditInstruction.OnClick().Bind(func(e *winc.Event) {
		showInstructionEditor(mainWindow, workDir, appendLog)
	})

	btnEditGuardRails.OnClick().Bind(func(e *winc.Event) {
		showGuardRailsEditor(mainWindow, workDir, appendLog)
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

	relayoutMain := func() {
		cw, ch := mainWindow.ClientWidth(), mainWindow.ClientHeight()
		if cw <= 0 || ch <= 0 {
			return
		}
		const logTop = 100
		const margin = 20
		w := cw - 2*margin
		h := ch - logTop - 10
		if w < 100 {
			w = 100
		}
		if h < 60 {
			h = 60
		}
		logEdit.SetPos(margin, logTop)
		logEdit.SetSize(w, h)
	}

	// [LOG-BUFFER] flush juga saat window di-resize (event UI thread)
	mainWindow.OnSize().Bind(func(e *winc.Event) {
		relayoutMain()
		flushLogs(logEdit)
	})

	mainWindow.Show()
	mainWindow.Center()
	relayoutMain()
	winc.RunMainLoop()
}

// ==================== PYTHON RESOLVER ====================

func resolvePythonEnv() PyEnv {
	venvDirs := []string{
		filepath.Join("..", ".venv"),
		filepath.Join("..", "..", ".venv"),
		".venv",
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

	return PyEnv{PythonCmd: "python"}
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

	importChecks := []struct {
		name string
		args []string
	}{
		{"Python Version", []string{"--version"}},
		{"ChromaDB", []string{"-c", "import chromadb; print('ChromaDB OK')"}},
		{"pdfplumber", []string{"-c", "import pdfplumber; print('pdfplumber OK')"}},
	}

	for _, c := range importChecks {
		logFn(fmt.Sprintf("\n[check] Checking %s...", c.name))
		if err := runCommand(ctx, logFn, workDir, env.PythonCmd, c.args...); err != nil {
			logFn(fmt.Sprintf("  ❌ %s unavailable", c.name))
		}
	}

	logFn("\n[check] Checking Neo4j connectivity...")

	neo4jCheckFile := filepath.Join(workDir, "_neo4j_check.py")
	if err := os.WriteFile(neo4jCheckFile, []byte(neo4jCheckScript), 0644); err != nil {
		logFn(fmt.Sprintf("  ❌ Cannot write Neo4j check script: %v", err))
		return nil
	}
	defer os.Remove(neo4jCheckFile)

	if err := runCommand(ctx, logFn, workDir, env.PythonCmd, neo4jCheckFile); err != nil {
		logFn("  ❌ Neo4j NOT reachable (lihat pesan di atas).")
	} else {
		logFn("  ✅ Neo4j reachable.")
	}

	return nil
}

// ==================== PROCESS TREE (WINDOWS JOB OBJECT) ====================

type procTree struct {
	cmd       *exec.Cmd
	jobHandle syscall.Handle
}

const (
	processSetQuota  = 0x0100
	processTerminate = 0x0001

	jobObjectExtendedLimitInformation = 9
	jobObjectLimitKillOnJobClose      = 0x2000
)

type jobObjectBasicLimitInformation struct {
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

type ioCounters struct {
	ReadOperationCount  uint64
	WriteOperationCount uint64
	OtherOperationCount uint64
	ReadTransferCount   uint64
	WriteTransferCount  uint64
	OtherTransferCount  uint64
}

type jobObjectExtendedLimitInformationStruct struct {
	BasicLimitInformation jobObjectBasicLimitInformation
	IoInfo                ioCounters
	ProcessMemoryLimit    uintptr
	JobMemoryLimit        uintptr
	PeakProcessMemoryUsed uintptr
	PeakJobMemoryUsed     uintptr
}

var (
	kernel32                     = syscall.NewLazyDLL("kernel32.dll")
	procCreateJobObjectW         = kernel32.NewProc("CreateJobObjectW")
	procSetInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	procAssignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	procTerminateJobObject       = kernel32.NewProc("TerminateJobObject")
	procCloseHandleK32           = kernel32.NewProc("CloseHandle")
)

func startInJobObject(cmd *exec.Cmd) (*procTree, error) {
	hJob, _, err := procCreateJobObjectW.Call(0, 0)
	if hJob == 0 {
		return nil, fmt.Errorf("CreateJobObjectW: %v", err)
	}

	info := jobObjectExtendedLimitInformationStruct{}
	info.BasicLimitInformation.LimitFlags = jobObjectLimitKillOnJobClose

	ret, _, err := procSetInformationJobObject.Call(
		hJob,
		uintptr(jobObjectExtendedLimitInformation),
		uintptr(unsafe.Pointer(&info)),
		unsafe.Sizeof(info),
	)
	if ret == 0 {
		procCloseHandleK32.Call(hJob)
		return nil, fmt.Errorf("SetInformationJobObject: %v", err)
	}

	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: 0x08000000,
	}

	if err := cmd.Start(); err != nil {
		procCloseHandleK32.Call(hJob)
		return nil, err
	}

	hProc, err := syscall.OpenProcess(
		processSetQuota|processTerminate,
		false,
		uint32(cmd.Process.Pid),
	)
	if err != nil {
		procCloseHandleK32.Call(hJob)
		_ = cmd.Process.Kill()
		return nil, fmt.Errorf("OpenProcess: %w", err)
	}
	defer syscall.CloseHandle(hProc)

	ret, _, err = procAssignProcessToJobObject.Call(
		hJob,
		uintptr(hProc),
	)
	if ret == 0 {
		procCloseHandleK32.Call(hJob)
		_ = cmd.Process.Kill()
		return nil, fmt.Errorf("AssignProcessToJobObject: %v", err)
	}

	return &procTree{cmd: cmd, jobHandle: syscall.Handle(hJob)}, nil
}

func (pt *procTree) Kill() {
	if pt == nil || pt.cmd == nil || pt.cmd.Process == nil {
		return
	}
	if pt.jobHandle != 0 {
		procTerminateJobObject.Call(uintptr(pt.jobHandle), 1)
		procCloseHandleK32.Call(uintptr(pt.jobHandle))
		pt.jobHandle = 0
	} else {
		_ = pt.cmd.Process.Kill()
	}
}

func (pt *procTree) Close() {
	if pt != nil && pt.jobHandle != 0 {
		procCloseHandleK32.Call(uintptr(pt.jobHandle))
		pt.jobHandle = 0
	}
}

// ==================== STREAMING EXEC COMMAND ====================

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
	cmd := exec.Command(name, args...)
	if workDir != "" {
		cmd.Dir = workDir
	}

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

	pt, err := startInJobObject(cmd)
	if err != nil {
		return err
	}

	killDone := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			logFn("\r\n[!] Killing process tree...")
			pt.Kill()
		case <-killDone:
		}
	}()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); streamPipe(stdout, logFn) }()
	go func() { defer wg.Done(); streamPipe(stderr, logFn) }()
	wg.Wait()

	err = cmd.Wait()
	close(killDone)
	pt.Close()

	if ctx.Err() != nil {
		return ctx.Err()
	}
	return err
}

// ==================== SHARED HELPERS ====================

func validateLangCode(code string) bool {
	code = strings.TrimSpace(strings.ToLower(code))
	if len(code) < 2 || len(code) > 5 {
		return false
	}
	matched, _ := regexp.MatchString(`^[a-z][a-z0-9_-]*$`, code)
	return matched
}

func buildLangList(existing map[string]map[string]string) []string {
	list := []string{}
	seen := map[string]bool{}
	for _, l := range defaultInstructionLangs {
		if _, ok := existing[l]; ok {
			list = append(list, l)
			seen[l] = true
		}
	}
	extra := []string{}
	for l := range existing {
		if !seen[l] {
			extra = append(extra, l)
		}
	}
	sort.Strings(extra)
	list = append(list, extra...)
	if len(list) == 0 {
		list = append(list, "id")
		existing["id"] = make(map[string]string)
	}
	return list
}

func buildGRLangList(existing map[string]map[string][]string) []string {
	list := []string{}
	seen := map[string]bool{}
	for _, l := range defaultInstructionLangs {
		if _, ok := existing[l]; ok {
			list = append(list, l)
			seen[l] = true
		}
	}
	extra := []string{}
	for l := range existing {
		if !seen[l] {
			extra = append(extra, l)
		}
	}
	sort.Strings(extra)
	list = append(list, extra...)
	if len(list) == 0 {
		list = append(list, "id")
		existing["id"] = make(map[string][]string)
	}
	return list
}

func orderSlotsForOutput(slots map[string]string) []string {
	ordered := []string{}
	seen := map[string]bool{}
	for _, s := range instructionSlots {
		if _, ok := slots[s]; ok {
			ordered = append(ordered, s)
			seen[s] = true
		}
	}
	extra := []string{}
	for s := range slots {
		if !seen[s] {
			extra = append(extra, s)
		}
	}
	sort.Strings(extra)
	ordered = append(ordered, extra...)
	return ordered
}

// ==================== INSTRUCTION SET EDITOR ====================

func readInstructionFile(path string) (map[string]map[string]string, error) {
	result := make(map[string]map[string]string)
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return result, nil
		}
		return result, err
	}
	defer f.Close()

	langRegex := regexp.MustCompile(`^##\s*LANG\s*:\s*(\S+)\s*$`)
	slotRegex := regexp.MustCompile(`^###\s*SLOT\s*:\s*(\S+)\s*$`)

	var currentLang, currentSlot string
	var buffer []string

	flushSlot := func() {
		if currentLang != "" && currentSlot != "" {
			text := strings.TrimSpace(strings.Join(buffer, "\n"))
			if text != "" {
				if result[currentLang] == nil {
					result[currentLang] = make(map[string]string)
				}
				result[currentLang][currentSlot] = text
			}
		}
		buffer = nil
	}

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if m := langRegex.FindStringSubmatch(line); m != nil {
			flushSlot()
			currentLang = strings.ToLower(strings.TrimSpace(m[1]))
			currentSlot = ""
			continue
		}
		if m := slotRegex.FindStringSubmatch(line); m != nil {
			flushSlot()
			currentSlot = strings.ToLower(strings.TrimSpace(m[1]))
			continue
		}
		if strings.TrimSpace(line) == "---" {
			flushSlot()
			currentSlot = ""
			continue
		}
		if currentSlot != "" {
			buffer = append(buffer, line)
		}
	}
	flushSlot()
	return result, scanner.Err()
}

func writeInstructionFile(path string, data map[string]map[string]string) error {
	var buf bytes.Buffer

	allLangs := []string{}
	seen := map[string]bool{}
	for _, l := range defaultInstructionLangs {
		if _, ok := data[l]; ok {
			allLangs = append(allLangs, l)
			seen[l] = true
		}
	}
	extraLangs := []string{}
	for l := range data {
		if !seen[l] {
			extraLangs = append(extraLangs, l)
		}
	}
	sort.Strings(extraLangs)
	allLangs = append(allLangs, extraLangs...)

	for _, lang := range allLangs {
		buf.WriteString(fmt.Sprintf("## LANG: %s\n\n", lang))
		slots := data[lang]

		for _, slot := range orderSlotsForOutput(slots) {
			buf.WriteString(fmt.Sprintf("### SLOT: %s\n", slot))
			buf.WriteString(slots[slot])
			buf.WriteString("\n\n")
		}
	}
	return os.WriteFile(path, buf.Bytes(), 0644)
}

// ==================== EMBED COMMAND (SHARED) ====================

func runPromptGuard(ctx context.Context, logFn func(string), workDir string) error {
	env := resolvePythonEnv()
	logFn("▶️  Running prompt_guard.py (instruction + guard rails)...")
	logFn("   (prompt_guard.py selalu membaca KEDUA file sekaligus)")

	err := runCommand(ctx, logFn, workDir,
		env.PythonCmd, "prompt_guard.py",
		"--instruction-file", instructionFileName,
		"--guard-rails-file", guardRailsFileName,
	)
	if err != nil {
		logFn(fmt.Sprintf("❌ Embed gagal: %v", err))
	} else {
		logFn("✅ Instruction + guard rails berhasil di-embed ke Neo4j.")
	}
	return err
}

// ==================== INSTRUCTION SET EDITOR ====================

func showInstructionEditor(parent *winc.Form, workDir string, logFn func(string)) {
	screenW, screenH := getScreenWorkArea()

	dlg := winc.NewForm(parent)
	dlg.SetText("Instruction Set Editor")
	dlg.SetSize(screenW, screenH)
	dlg.SetMinSize(800, 520)
	dlg.Center()

	layout := computeEditorLayout(dlg.ClientWidth(), dlg.ClientHeight())

	filePath := filepath.Join(workDir, instructionFileName)

	existing, _ := readInstructionFile(filePath)
	if len(existing) == 0 {
		for _, l := range defaultInstructionLangs {
			existing[l] = make(map[string]string)
		}
	}

	langList := buildLangList(existing)
	currentLang := langList[0]

	lblLangBar := winc.NewLabel(dlg)
	lblLangBar.SetText("Languages:")
	lblLangBar.SetPos(layout.LangLabelX, layout.TopBarY+5)
	lblLangBar.SetSize(layout.LangLabelW, layout.LangLabelH)

	langButtons := make([]*winc.PushButton, maxLangButtons)
	langButtonCodes := make([]string, maxLangButtons)
	for i := 0; i < maxLangButtons; i++ {
		btn := winc.NewPushButton(dlg)
		btn.SetPos(-500, -500)
		btn.SetSize(1, 1)
		btn.SetText("")
		btn.SetEnabled(false)
		langButtons[i] = btn
		langButtonCodes[i] = ""
	}

	langInput := winc.NewEdit(dlg)
	langInput.SetPos(layout.InputX, layout.InputY)
	langInput.SetSize(layout.InputW, layout.InputH)
	langInput.SetText("")

	lblAddHint := winc.NewLabel(dlg)
	lblAddHint.SetText("e.g. jp, ar, zh")
	lblAddHint.SetPos(layout.InputX, layout.InputY+layout.InputH+2)
	lblAddHint.SetSize(layout.InputW, 16)

	btnAddLang := winc.NewPushButton(dlg)
	btnAddLang.SetText("➕ Add")
	btnAddLang.SetPos(layout.AddBtnX, layout.TopBarY)
	btnAddLang.SetSize(layout.AddBtnW, layout.AddBtnH)

	btnRemoveLang := winc.NewPushButton(dlg)
	btnRemoveLang.SetText("✖ Remove")
	btnRemoveLang.SetPos(layout.RemoveBtnX, layout.TopBarY)
	btnRemoveLang.SetSize(layout.RemoveBtnW, layout.RemoveBtnH)

	fields := make(map[string]*winc.MultiEdit)
	slotLabels := make([]*winc.Label, 0, len(instructionSlots))
	for i, slot := range instructionSlots {
		y := layout.SlotStartY + i*(layout.SlotFieldHeight+layout.SlotFieldGap)

		lbl := winc.NewLabel(dlg)
		lbl.SetText(slot)
		lbl.SetPos(layout.SlotLabelX, y)
		lbl.SetSize(layout.SlotLabelW, layout.SlotLabelH)

		slotLabels = append(slotLabels, lbl)

		me := winc.NewMultiEdit(dlg)
		me.SetPos(layout.SlotFieldX, y+layout.SlotLabelH)
		me.SetSize(layout.SlotFieldW, layout.SlotFieldHeight-layout.SlotLabelH)
		fields[slot] = me
	}

	collectCurrentFields := func() map[string]string {
		out := make(map[string]string)
		for _, slot := range instructionSlots {
			txt := strings.TrimSpace(fields[slot].Text())
			if txt != "" {
				out[slot] = txt
			}
		}
		return out
	}

	saveCurrentLangFields := func() {
		if existing[currentLang] == nil {
			existing[currentLang] = make(map[string]string)
		}
		existing[currentLang] = collectCurrentFields()
	}

	loadLangIntoFields := func(lang string) {
		slotsMap := existing[lang]
		for _, slot := range instructionSlots {
			me := fields[slot]
			if slotsMap != nil {
				if txt, ok := slotsMap[slot]; ok {
					me.SetText(txt)
					continue
				}
			}
			me.SetText("")
		}
	}

	refreshLangButtons := func() {
		for i := 0; i < maxLangButtons; i++ {
			btn := langButtons[i]
			if i < len(langList) {
				code := langList[i]
				langButtonCodes[i] = code
				btn.SetEnabled(true)
				btn.SetPos(
					layout.LangBtnStartX+i*(layout.LangBtnWidth+layout.LangBtnGap),
					layout.TopBarY,
				)
				btn.SetSize(layout.LangBtnWidth, layout.LangLabelH+8)
				if code == currentLang {
					btn.SetText("●" + code)
				} else {
					btn.SetText(" " + code)
				}
			} else {
				langButtonCodes[i] = ""
				btn.SetEnabled(false)
				btn.SetText("")
				btn.SetPos(-500, -500)
				btn.SetSize(1, 1)
			}
		}
	}

	switchLang := func(newLang string) {
		if newLang == currentLang {
			return
		}
		if _, ok := existing[newLang]; !ok {
			return
		}
		saveCurrentLangFields()
		currentLang = newLang
		loadLangIntoFields(currentLang)
		refreshLangButtons()
	}

	for i := 0; i < maxLangButtons; i++ {
		idx := i
		langButtons[i].OnClick().Bind(func(e *winc.Event) {
			code := langButtonCodes[idx]
			if code == "" {
				return
			}
			switchLang(code)
		})
	}

	btnAddLang.OnClick().Bind(func(e *winc.Event) {
		raw := langInput.Text()
		if raw == "" {
			raw = getWindowText(langInput.Handle())
		}
		code := strings.ToLower(strings.TrimSpace(raw))

		if code == "" {
			logFn("⚠️  Kode bahasa kosong.")
			return
		}
		if !validateLangCode(code) {
			logFn(fmt.Sprintf("⚠️  Kode '%s' tidak valid.", code))
			return
		}
		if _, ok := existing[code]; ok {
			logFn(fmt.Sprintf("ℹ️  Bahasa '%s' sudah ada.", code))
			return
		}
		if len(existing) >= maxLangButtons {
			logFn(fmt.Sprintf("⚠️  Maksimal %d bahasa.", maxLangButtons))
			return
		}

		saveCurrentLangFields()
		existing[code] = make(map[string]string)
		langList = buildLangList(existing)
		langInput.SetText("")
		currentLang = code
		loadLangIntoFields(currentLang)
		refreshLangButtons()
		logFn(fmt.Sprintf("✅ Bahasa '%s' ditambahkan.", code))
	})

	btnRemoveLang.OnClick().Bind(func(e *winc.Event) {
		if len(existing) <= 1 {
			logFn("⚠️  Minimal harus ada 1 bahasa.")
			return
		}
		lang := currentLang
		delete(existing, lang)
		langList = buildLangList(existing)
		currentLang = langList[0]
		loadLangIntoFields(currentLang)
		refreshLangButtons()
		logFn(fmt.Sprintf("🗑️  Bahasa '%s' dihapus.", lang))
	})

	loadLangIntoFields(currentLang)
	refreshLangButtons()

	btnSave := winc.NewPushButton(dlg)
	btnSave.SetText("💾 Save to instruction_set.md")
	btnSave.SetPos(layout.SaveBtnX, layout.BottomBtnY)
	btnSave.SetSize(layout.SaveBtnW, layout.BottomBtnH)
	btnSave.OnClick().Bind(func(e *winc.Event) {
		saveCurrentLangFields()
		if err := writeInstructionFile(filePath, existing); err != nil {
			logFn(fmt.Sprintf("❌ Gagal menyimpan: %v", err))
			return
		}
		logFn(fmt.Sprintf("✅ Instruction saved to %s", instructionFileName))
	})

	btnEmbed := winc.NewPushButton(dlg)
	btnEmbed.SetText("💾🚀 Save & Embed (Both Files)")
	btnEmbed.SetPos(layout.EmbedBtnX, layout.BottomBtnY)
	btnEmbed.SetSize(layout.EmbedBtnW, layout.BottomBtnH)
	btnEmbed.OnClick().Bind(func(e *winc.Event) {
		saveCurrentLangFields()
		if err := writeInstructionFile(filePath, existing); err != nil {
			logFn(fmt.Sprintf("❌ Gagal menyimpan instruction: %v", err))
			return
		}
		logFn(fmt.Sprintf("✅ Instruction saved to %s", instructionFileName))

		go runPromptGuard(context.Background(), logFn, workDir)
		dlg.Close()
	})

	btnClose := winc.NewPushButton(dlg)
	btnClose.SetText("✖ Close")
	btnClose.SetPos(layout.CloseBtnX, layout.BottomBtnY)
	btnClose.SetSize(layout.CloseBtnW, layout.BottomBtnH)
	btnClose.OnClick().Bind(func(e *winc.Event) {
		dlg.Close()
	})

	relayout := func() {
		cw, ch := dlg.ClientWidth(), dlg.ClientHeight()
		if cw <= 0 || ch <= 0 {
			return
		}
		layout = computeEditorLayout(cw, ch)

		lblLangBar.SetPos(layout.LangLabelX, layout.TopBarY+5)
		lblLangBar.SetSize(layout.LangLabelW, layout.LangLabelH)
		langInput.SetPos(layout.InputX, layout.InputY)
		langInput.SetSize(layout.InputW, layout.InputH)
		lblAddHint.SetPos(layout.InputX, layout.InputY+layout.InputH+2)
		lblAddHint.SetSize(layout.InputW, 16)
		btnAddLang.SetPos(layout.AddBtnX, layout.TopBarY)
		btnAddLang.SetSize(layout.AddBtnW, layout.AddBtnH)
		btnRemoveLang.SetPos(layout.RemoveBtnX, layout.TopBarY)
		btnRemoveLang.SetSize(layout.RemoveBtnW, layout.RemoveBtnH)
		refreshLangButtons()

		for i, slot := range instructionSlots {
			y := layout.SlotStartY + i*(layout.SlotFieldHeight+layout.SlotFieldGap)
			slotLabels[i].SetPos(layout.SlotLabelX, y)
			slotLabels[i].SetSize(layout.SlotLabelW, layout.SlotLabelH)
			fields[slot].SetPos(layout.SlotFieldX, y+layout.SlotLabelH)
			fields[slot].SetSize(layout.SlotFieldW, layout.SlotFieldHeight-layout.SlotLabelH)
		}

		btnSave.SetPos(layout.SaveBtnX, layout.BottomBtnY)
		btnSave.SetSize(layout.SaveBtnW, layout.BottomBtnH)
		btnEmbed.SetPos(layout.EmbedBtnX, layout.BottomBtnY)
		btnEmbed.SetSize(layout.EmbedBtnW, layout.BottomBtnH)
		btnClose.SetPos(layout.CloseBtnX, layout.BottomBtnY)
		btnClose.SetSize(layout.CloseBtnW, layout.BottomBtnH)
	}
	dlg.OnSize().Bind(func(e *winc.Event) {
		relayout()
	})

	dlg.Show()
	relayout()
}

// ==================== GUARD RAILS EDITOR ====================

func readGuardRailsFile(path string) (map[string]map[string][]string, error) {
	result := make(map[string]map[string][]string)
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return result, nil
		}
		return result, err
	}
	defer f.Close()

	langRegex := regexp.MustCompile(`^##\s*LANG\s*:\s*(\S+)\s*$`)
	slotRegex := regexp.MustCompile(`^###\s*SLOT\s*:\s*(\S+)\s*$`)

	var currentLang, currentSlot string

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if m := langRegex.FindStringSubmatch(line); m != nil {
			currentLang = strings.ToLower(strings.TrimSpace(m[1]))
			currentSlot = ""
			continue
		}
		if m := slotRegex.FindStringSubmatch(line); m != nil {
			currentSlot = strings.ToLower(strings.TrimSpace(m[1]))
			continue
		}

		stripped := strings.TrimSpace(line)
		if stripped == "" || strings.HasPrefix(stripped, "#") {
			continue
		}
		if !strings.HasPrefix(stripped, "-") {
			continue
		}
		pattern := strings.TrimSpace(stripped[1:])
		if pattern == "" {
			continue
		}
		if currentLang == "" || currentSlot == "" {
			continue
		}
		if result[currentLang] == nil {
			result[currentLang] = make(map[string][]string)
		}
		result[currentLang][currentSlot] = append(
			result[currentLang][currentSlot], pattern,
		)
	}
	return result, scanner.Err()
}

func writeGuardRailsFile(path string, data map[string]map[string][]string) error {
	var buf bytes.Buffer

	allLangs := []string{}
	seen := map[string]bool{}
	for _, l := range defaultInstructionLangs {
		if _, ok := data[l]; ok {
			allLangs = append(allLangs, l)
			seen[l] = true
		}
	}
	extraLangs := []string{}
	for l := range data {
		if !seen[l] {
			extraLangs = append(extraLangs, l)
		}
	}
	sort.Strings(extraLangs)
	allLangs = append(allLangs, extraLangs...)

	for _, lang := range allLangs {
		buf.WriteString(fmt.Sprintf("## LANG: %s\n\n", lang))
		slots := data[lang]

		ordered := []string{}
		seenSlot := map[string]bool{}
		for _, s := range instructionSlots {
			if _, ok := slots[s]; ok {
				ordered = append(ordered, s)
				seenSlot[s] = true
			}
		}
		extraSlots := []string{}
		for s := range slots {
			if !seenSlot[s] {
				extraSlots = append(extraSlots, s)
			}
		}
		sort.Strings(extraSlots)
		ordered = append(ordered, extraSlots...)

		for _, slot := range ordered {
			buf.WriteString(fmt.Sprintf("### SLOT: %s\n", slot))
			for _, p := range slots[slot] {
				buf.WriteString(fmt.Sprintf("- %s\n", p))
			}
			buf.WriteString("\n")
		}
	}
	return os.WriteFile(path, buf.Bytes(), 0644)
}

func showGuardRailsEditor(parent *winc.Form, workDir string, logFn func(string)) {
	screenW, screenH := getScreenWorkArea()

	dlg := winc.NewForm(parent)
	dlg.SetText("Guard Rails Editor — Forbidden Patterns")
	dlg.SetSize(screenW, screenH)
	dlg.SetMinSize(800, 520)
	dlg.Center()

	layout := computeEditorLayout(dlg.ClientWidth(), dlg.ClientHeight())

	filePath := filepath.Join(workDir, guardRailsFileName)

	existing, _ := readGuardRailsFile(filePath)
	if len(existing) == 0 {
		for _, l := range defaultInstructionLangs {
			existing[l] = make(map[string][]string)
		}
	}

	langList := buildGRLangList(existing)
	currentLang := langList[0]

	lblLangBar := winc.NewLabel(dlg)
	lblLangBar.SetText("Languages:")
	lblLangBar.SetPos(layout.LangLabelX, layout.TopBarY+5)
	lblLangBar.SetSize(layout.LangLabelW, layout.LangLabelH)

	langButtons := make([]*winc.PushButton, maxLangButtons)
	langButtonCodes := make([]string, maxLangButtons)
	for i := 0; i < maxLangButtons; i++ {
		btn := winc.NewPushButton(dlg)
		btn.SetPos(-500, -500)
		btn.SetSize(1, 1)
		btn.SetText("")
		btn.SetEnabled(false)
		langButtons[i] = btn
		langButtonCodes[i] = ""
	}

	langInput := winc.NewEdit(dlg)
	langInput.SetPos(layout.InputX, layout.InputY)
	langInput.SetSize(layout.InputW, layout.InputH)
	langInput.SetText("")

	lblAddHint := winc.NewLabel(dlg)
	lblAddHint.SetText("e.g. jp, ar, zh")
	lblAddHint.SetPos(layout.InputX, layout.InputY+layout.InputH+2)
	lblAddHint.SetSize(layout.InputW, 16)

	btnAddLang := winc.NewPushButton(dlg)
	btnAddLang.SetText("➕ Add")
	btnAddLang.SetPos(layout.AddBtnX, layout.TopBarY)
	btnAddLang.SetSize(layout.AddBtnW, layout.AddBtnH)

	btnRemoveLang := winc.NewPushButton(dlg)
	btnRemoveLang.SetText("✖ Remove")
	btnRemoveLang.SetPos(layout.RemoveBtnX, layout.TopBarY)
	btnRemoveLang.SetSize(layout.RemoveBtnW, layout.RemoveBtnH)

	fields := make(map[string]*winc.MultiEdit)
	slotLabels := make([]*winc.Label, 0, len(instructionSlots))
	for i, slot := range instructionSlots {
		y := layout.SlotStartY + i*(layout.SlotFieldHeight+layout.SlotFieldGap)

		lbl := winc.NewLabel(dlg)
		lbl.SetText(slot + "   (1 pattern per baris)")
		lbl.SetPos(layout.SlotLabelX, y)
		lbl.SetSize(layout.SlotLabelW, layout.SlotLabelH)

		slotLabels = append(slotLabels, lbl)

		me := winc.NewMultiEdit(dlg)
		me.SetPos(layout.SlotFieldX, y+layout.SlotLabelH)
		me.SetSize(layout.SlotFieldW, layout.SlotFieldHeight-layout.SlotLabelH)
		fields[slot] = me
	}

	collectCurrentPatterns := func() map[string][]string {
		out := make(map[string][]string)
		for _, slot := range instructionSlots {
			txt := fields[slot].Text()
			lines := strings.Split(txt, "\n")
			patterns := []string{}
			for _, line := range lines {
				p := strings.TrimSpace(line)
				if p == "" {
					continue
				}
				if strings.HasPrefix(p, "-") {
					p = strings.TrimSpace(p[1:])
				}
				if p != "" {
					patterns = append(patterns, p)
				}
			}
			if len(patterns) > 0 {
				out[slot] = patterns
			}
		}
		return out
	}

	saveCurrentLangFields := func() {
		if existing[currentLang] == nil {
			existing[currentLang] = make(map[string][]string)
		}
		existing[currentLang] = collectCurrentPatterns()
	}

	loadLangIntoFields := func(lang string) {
		slotsMap := existing[lang]
		for _, slot := range instructionSlots {
			me := fields[slot]
			if slotsMap != nil {
				if patterns, ok := slotsMap[slot]; ok && len(patterns) > 0 {
					me.SetText(strings.Join(patterns, "\r\n"))
					continue
				}
			}
			me.SetText("")
		}
	}

	refreshLangButtons := func() {
		for i := 0; i < maxLangButtons; i++ {
			btn := langButtons[i]
			if i < len(langList) {
				code := langList[i]
				langButtonCodes[i] = code
				btn.SetEnabled(true)
				btn.SetPos(
					layout.LangBtnStartX+i*(layout.LangBtnWidth+layout.LangBtnGap),
					layout.TopBarY,
				)
				btn.SetSize(layout.LangBtnWidth, layout.LangLabelH+8)
				if code == currentLang {
					btn.SetText("●" + code)
				} else {
					btn.SetText(" " + code)
				}
			} else {
				langButtonCodes[i] = ""
				btn.SetEnabled(false)
				btn.SetText("")
				btn.SetPos(-500, -500)
				btn.SetSize(1, 1)
			}
		}
	}

	switchLang := func(newLang string) {
		if newLang == currentLang {
			return
		}
		if _, ok := existing[newLang]; !ok {
			return
		}
		saveCurrentLangFields()
		currentLang = newLang
		loadLangIntoFields(currentLang)
		refreshLangButtons()
	}

	for i := 0; i < maxLangButtons; i++ {
		idx := i
		langButtons[i].OnClick().Bind(func(e *winc.Event) {
			code := langButtonCodes[idx]
			if code == "" {
				return
			}
			switchLang(code)
		})
	}

	btnAddLang.OnClick().Bind(func(e *winc.Event) {
		raw := langInput.Text()
		if raw == "" {
			raw = getWindowText(langInput.Handle())
		}
		code := strings.ToLower(strings.TrimSpace(raw))

		if code == "" {
			logFn("⚠️  Kode bahasa kosong.")
			return
		}
		if !validateLangCode(code) {
			logFn(fmt.Sprintf("⚠️  Kode '%s' tidak valid.", code))
			return
		}
		if _, ok := existing[code]; ok {
			logFn(fmt.Sprintf("ℹ️  Bahasa '%s' sudah ada.", code))
			return
		}
		if len(existing) >= maxLangButtons {
			logFn(fmt.Sprintf("⚠️  Maksimal %d bahasa.", maxLangButtons))
			return
		}

		saveCurrentLangFields()
		existing[code] = make(map[string][]string)
		langList = buildGRLangList(existing)
		langInput.SetText("")
		currentLang = code
		loadLangIntoFields(currentLang)
		refreshLangButtons()
		logFn(fmt.Sprintf("✅ Bahasa '%s' ditambahkan.", code))
	})

	btnRemoveLang.OnClick().Bind(func(e *winc.Event) {
		if len(existing) <= 1 {
			logFn("⚠️  Minimal harus ada 1 bahasa.")
			return
		}
		lang := currentLang
		delete(existing, lang)
		langList = buildGRLangList(existing)
		currentLang = langList[0]
		loadLangIntoFields(currentLang)
		refreshLangButtons()
		logFn(fmt.Sprintf("🗑️  Bahasa '%s' dihapus.", lang))
	})

	loadLangIntoFields(currentLang)
	refreshLangButtons()

	btnSave := winc.NewPushButton(dlg)
	btnSave.SetText("💾 Save to guard_rails.md")
	btnSave.SetPos(layout.SaveBtnX, layout.BottomBtnY)
	btnSave.SetSize(layout.SaveBtnW, layout.BottomBtnH)
	btnSave.OnClick().Bind(func(e *winc.Event) {
		saveCurrentLangFields()
		if err := writeGuardRailsFile(filePath, existing); err != nil {
			logFn(fmt.Sprintf("❌ Gagal menyimpan: %v", err))
			return
		}
		logFn(fmt.Sprintf("✅ Guard rails saved to %s", guardRailsFileName))
	})

	btnEmbed := winc.NewPushButton(dlg)
	btnEmbed.SetText("💾🚀 Save & Embed (Both Files)")
	btnEmbed.SetPos(layout.EmbedBtnX, layout.BottomBtnY)
	btnEmbed.SetSize(layout.EmbedBtnW, layout.BottomBtnH)
	btnEmbed.OnClick().Bind(func(e *winc.Event) {
		saveCurrentLangFields()
		if err := writeGuardRailsFile(filePath, existing); err != nil {
			logFn(fmt.Sprintf("❌ Gagal menyimpan guard rails: %v", err))
			return
		}
		logFn(fmt.Sprintf("✅ Guard rails saved to %s", guardRailsFileName))

		go runPromptGuard(context.Background(), logFn, workDir)
		dlg.Close()
	})

	btnClose := winc.NewPushButton(dlg)
	btnClose.SetText("✖ Close")
	btnClose.SetPos(layout.CloseBtnX, layout.BottomBtnY)
	btnClose.SetSize(layout.CloseBtnW, layout.BottomBtnH)
	btnClose.OnClick().Bind(func(e *winc.Event) {
		dlg.Close()
	})

	relayout := func() {
		cw, ch := dlg.ClientWidth(), dlg.ClientHeight()
		if cw <= 0 || ch <= 0 {
			return
		}
		layout = computeEditorLayout(cw, ch)

		lblLangBar.SetPos(layout.LangLabelX, layout.TopBarY+5)
		lblLangBar.SetSize(layout.LangLabelW, layout.LangLabelH)
		langInput.SetPos(layout.InputX, layout.InputY)
		langInput.SetSize(layout.InputW, layout.InputH)
		lblAddHint.SetPos(layout.InputX, layout.InputY+layout.InputH+2)
		lblAddHint.SetSize(layout.InputW, 16)
		btnAddLang.SetPos(layout.AddBtnX, layout.TopBarY)
		btnAddLang.SetSize(layout.AddBtnW, layout.AddBtnH)
		btnRemoveLang.SetPos(layout.RemoveBtnX, layout.TopBarY)
		btnRemoveLang.SetSize(layout.RemoveBtnW, layout.RemoveBtnH)
		refreshLangButtons()

		for i, slot := range instructionSlots {
			y := layout.SlotStartY + i*(layout.SlotFieldHeight+layout.SlotFieldGap)
			slotLabels[i].SetPos(layout.SlotLabelX, y)
			slotLabels[i].SetSize(layout.SlotLabelW, layout.SlotLabelH)
			fields[slot].SetPos(layout.SlotFieldX, y+layout.SlotLabelH)
			fields[slot].SetSize(layout.SlotFieldW, layout.SlotFieldHeight-layout.SlotLabelH)
		}

		btnSave.SetPos(layout.SaveBtnX, layout.BottomBtnY)
		btnSave.SetSize(layout.SaveBtnW, layout.BottomBtnH)
		btnEmbed.SetPos(layout.EmbedBtnX, layout.BottomBtnY)
		btnEmbed.SetSize(layout.EmbedBtnW, layout.BottomBtnH)
		btnClose.SetPos(layout.CloseBtnX, layout.BottomBtnY)
		btnClose.SetSize(layout.CloseBtnW, layout.BottomBtnH)
	}
	dlg.OnSize().Bind(func(e *winc.Event) {
		relayout()
	})

	dlg.Show()
	relayout()
}