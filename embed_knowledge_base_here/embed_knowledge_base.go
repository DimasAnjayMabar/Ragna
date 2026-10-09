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
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"github.com/tadvi/winc"
)

// Win32 API setup
var (
	user32                    = syscall.NewLazyDLL("user32.dll")
	procSendMessageW          = user32.NewProc("SendMessageW")
	procGetWindowTextW        = user32.NewProc("GetWindowTextW")
	procSystemParametersInfoW = user32.NewProc("SystemParametersInfoW")
)

const (
	WM_VSCROLL     = 0x0115
	EM_SETSEL      = 0x00B1
	EM_REPLACESEL  = 0x00C2
	EM_SCROLLCARET = 0x00B7
	SB_BOTTOM      = 7
	SPI_GETWORKAREA = 0x0030
)

// RECT struct untuk Win32 SystemParametersInfo
type RECT struct {
	Left   int32
	Top    int32
	Right  int32
	Bottom int32
}

// getScreenWorkArea mengembalikan ukuran working area layar (tanpa taskbar).
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
		// Fallback jika gagal
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
)

var (
	instructionSlots = []string{
		"bot_name",
		"bot_identity",
		"social_guard_rail",
		"knowledge_guard_rail",
		"memory_block_guard_rail",
		"memory_summary_block",
	}
	defaultInstructionLangs = []string{"id", "en"}
)

// ==================== EDITOR LAYOUT ====================

// EditorLayout menyimpan posisi & ukuran elemen editor.
// Dihitung dinamis dari lebar/tinggi window.
type EditorLayout struct {
	WindowWidth     int
	WindowHeight    int

	TopBarY         int
	TopBarH         int

	LangLabelX      int
	LangLabelW      int
	LangLabelH      int

	LangBtnStartX   int
	LangBtnWidth    int
	LangBtnGap      int

	InputX          int
	InputY          int
	InputW          int
	InputH          int

	AddBtnX         int
	AddBtnW         int
	AddBtnH         int

	RemoveBtnX      int
	RemoveBtnW      int
	RemoveBtnH      int

	SlotStartY      int
	SlotLabelH      int
	SlotFieldHeight int
	SlotFieldGap    int
	SlotLabelX      int
	SlotLabelW      int
	SlotFieldX      int
	SlotFieldW      int

	BottomBtnY      int
	BottomBtnH      int
	SaveBtnX        int
	SaveBtnW        int
	EmbedBtnX       int
	EmbedBtnW       int
	CloseBtnX       int
	CloseBtnW       int
}

// computeEditorLayout menghitung posisi & ukuran elemen editor
// berdasarkan ukuran AREA KLIEN window (bukan ukuran luar window).
//
// PENTING: winc SetSize() pada Form mengatur ukuran LUAR window
// (termasuk title bar + border). Jika ukuran itu dipakai sebagai tinggi
// layout, tombol bawah akan terdorong keluar dari area yang terlihat.
// Karena itu selalu kirim dlg.ClientWidth() / dlg.ClientHeight().
func computeEditorLayout(winW, winH int) EditorLayout {
	const (
		marginX      = 10
		marginTop    = 10
		marginBottom = 10
		labelHeight  = 20 // tinggi label di atas field
		minFieldH    = 30 // tinggi minimum area teks per slot
	)

	// ── Top bar ───────────────────────────────────────────────────────
	topBarY := marginTop
	topBarH := 32

	langLabelX := marginX
	langLabelW := 70
	langLabelH := 22

	langBtnStartX := langLabelX + langLabelW + 5
	langBtnWidth := 55
	langBtnGap := 4

	// Blok kanan: input(110) + gap(10) + Add(80) + gap(10) + Remove(90)
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

	// ── Bottom buttons (selalu menempel di dasar area klien) ──────────
	bottomBtnH := 32
	bottomBtnY := winH - marginBottom - bottomBtnH

	saveBtnX := marginX
	saveBtnW := 220

	embedBtnX := saveBtnX + saveBtnW + 10
	embedBtnW := 380

	closeBtnW := 130
	closeBtnX := winW - marginX - closeBtnW

	// ── Slot fields ───────────────────────────────────────────────────
	// Mulai di bawah top bar + ruang untuk hint (25px)
	slotStartY := topBarY + topBarH + 25

	// Ruang tersedia = dari slotStartY sampai sedikit di atas tombol bawah
	slotAreaBottom := bottomBtnY - 8
	availableHeight := slotAreaBottom - slotStartY

	numSlots := len(instructionSlots)
	slotFieldGap := 6

	// Tinggi satu blok slot (label + field), dibagi rata.
	// Tidak ada batas maksimum: field ikut membesar saat window diperbesar.
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
	// Dapatkan screen work area
	screenW, screenH := getScreenWorkArea()

	mainWindow := winc.NewForm(nil)
	mainWindow.SetSize(screenW, screenH)
	mainWindow.SetMinSize(1000, 480) // lebar minimum agar semua tombol atas muat
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
	logEdit.SetSize(screenW-40, screenH-140) // ukuran awal; disesuaikan oleh relayoutMain

	appendLog := func(msg string) {
		if msg == "" {
			return
		}
		text := msg + "\r\n"
		ptr, err := syscall.UTF16PtrFromString(text)
		if err != nil {
			return
		}

		sendMessage(logEdit.Handle(), EM_SETSEL, ^uintptr(0), ^uintptr(0))
		sendMessage(logEdit.Handle(), EM_REPLACESEL, 0, uintptr(unsafe.Pointer(ptr)))
		sendMessage(logEdit.Handle(), EM_SCROLLCARET, 0, 0)
		sendMessage(logEdit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
	}

	allButtons := []*winc.PushButton{
		btnCheckEnv, btnEmbed, btnDeleteKB, btnDeleteMemory, btnClear,
		btnEditInstruction, btnEditGuardRails,
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

	// Log area mengisi sisa ruang klien (di bawah baris tombol) secara dinamis.
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
	mainWindow.OnSize().Bind(func(e *winc.Event) {
		relayoutMain()
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
		CreationFlags: 0x08000000,
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

// runPromptGuard menjalankan prompt_guard.py dengan KEDUA file.
// Ini adalah SATU-SATUNYA fungsi yang dipakai oleh Save & Embed
// di kedua editor — memastikan konsistensi.
func runPromptGuard(logFn func(string), workDir string) error {
	env := resolvePythonEnv()
	logFn("▶️  Running prompt_guard.py (instruction + guard rails)...")
	logFn("   (prompt_guard.py selalu membaca KEDUA file sekaligus)")

	err := runCommand(context.Background(), logFn, workDir,
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
	dlg.SetMinSize(800, 520) // cegah window terlalu kecil sehingga tombol bawah tertutup
	dlg.Center()

	// Layout dihitung dari ukuran AREA KLIEN (bukan ukuran luar window)
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

	// ── BARIS 1: Language selector ────────────────────────────────────
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

	// ── SLOT FIELDS ───────────────────────────────────────────────────
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

	// ── Helpers ───────────────────────────────────────────────────────
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

	// ── TOMBOL BAWAH ──────────────────────────────────────────────────
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
		// 1) Simpan instruction_set.md dari field yang aktif
		saveCurrentLangFields()
		if err := writeInstructionFile(filePath, existing); err != nil {
			logFn(fmt.Sprintf("❌ Gagal menyimpan instruction: %v", err))
			return
		}
		logFn(fmt.Sprintf("✅ Instruction saved to %s", instructionFileName))

		// 2) Jalankan prompt_guard.py — baca KEDUA file
		// (guard_rails.md diambil dari file yang ada di disk, TIDAK dari editor ini)
		go runPromptGuard(logFn, workDir)

		dlg.Close()
	})

	btnClose := winc.NewPushButton(dlg)
	btnClose.SetText("✖ Close")
	btnClose.SetPos(layout.CloseBtnX, layout.BottomBtnY)
	btnClose.SetSize(layout.CloseBtnW, layout.BottomBtnH)
	btnClose.OnClick().Bind(func(e *winc.Event) {
		dlg.Close()
	})

	// ── RELAYOUT DINAMIS ──────────────────────────────────────────────
	// Dipanggil saat window di-resize / di-maximize / pertama kali tampil.
	relayout := func() {
		cw, ch := dlg.ClientWidth(), dlg.ClientHeight()
		if cw <= 0 || ch <= 0 {
			return // window sedang di-minimize
		}
		layout = computeEditorLayout(cw, ch)

		// Top bar
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
		refreshLangButtons() // posisi tombol bahasa memakai `layout` terbaru

		// Slot fields
		for i, slot := range instructionSlots {
			y := layout.SlotStartY + i*(layout.SlotFieldHeight+layout.SlotFieldGap)
			slotLabels[i].SetPos(layout.SlotLabelX, y)
			slotLabels[i].SetSize(layout.SlotLabelW, layout.SlotLabelH)
			fields[slot].SetPos(layout.SlotFieldX, y+layout.SlotLabelH)
			fields[slot].SetSize(layout.SlotFieldW, layout.SlotFieldHeight-layout.SlotLabelH)
		}

		// Tombol bawah (selalu menempel di dasar area klien)
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
	dlg.SetMinSize(800, 520) // cegah window terlalu kecil sehingga tombol bawah tertutup
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

	// ── TOMBOL BAWAH ──────────────────────────────────────────────────
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
		// 1) Simpan guard_rails.md dari field yang aktif
		saveCurrentLangFields()
		if err := writeGuardRailsFile(filePath, existing); err != nil {
			logFn(fmt.Sprintf("❌ Gagal menyimpan guard rails: %v", err))
			return
		}
		logFn(fmt.Sprintf("✅ Guard rails saved to %s", guardRailsFileName))

		// 2) Jalankan prompt_guard.py — baca KEDUA file
		// (instruction_set.md diambil dari file yang ada di disk)
		go runPromptGuard(logFn, workDir)

		dlg.Close()
	})

	btnClose := winc.NewPushButton(dlg)
	btnClose.SetText("✖ Close")
	btnClose.SetPos(layout.CloseBtnX, layout.BottomBtnY)
	btnClose.SetSize(layout.CloseBtnW, layout.BottomBtnH)
	btnClose.OnClick().Bind(func(e *winc.Event) {
		dlg.Close()
	})

	// ── RELAYOUT DINAMIS ──────────────────────────────────────────────
	// Dipanggil saat window di-resize / di-maximize / pertama kali tampil.
	relayout := func() {
		cw, ch := dlg.ClientWidth(), dlg.ClientHeight()
		if cw <= 0 || ch <= 0 {
			return // window sedang di-minimize
		}
		layout = computeEditorLayout(cw, ch)

		// Top bar
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
		refreshLangButtons() // posisi tombol bahasa memakai `layout` terbaru

		// Slot fields
		for i, slot := range instructionSlots {
			y := layout.SlotStartY + i*(layout.SlotFieldHeight+layout.SlotFieldGap)
			slotLabels[i].SetPos(layout.SlotLabelX, y)
			slotLabels[i].SetSize(layout.SlotLabelW, layout.SlotLabelH)
			fields[slot].SetPos(layout.SlotFieldX, y+layout.SlotLabelH)
			fields[slot].SetSize(layout.SlotFieldW, layout.SlotFieldHeight-layout.SlotLabelH)
		}

		// Tombol bawah (selalu menempel di dasar area klien)
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