package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"github.com/tadvi/winc"
)

// Win32 API setup for real-time log streaming & auto-scrolling
// (same pattern as embed_knowledge_base.go)
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

func main() {
	mainWindow := winc.NewForm(nil)
	mainWindow.SetSize(900, 700)
	mainWindow.SetText("RAG Pipeline Evaluator")

	lblTitle := winc.NewLabel(mainWindow)
	lblTitle.SetPos(20, 15)
	lblTitle.SetSize(400, 24)
	lblTitle.SetText("RAG Pipeline Evaluator")

	lblSub := winc.NewLabel(mainWindow)
	lblSub.SetPos(20, 45)
	lblSub.SetSize(400, 20)
	lblSub.SetText("Pilih evaluasi yang ingin dijalankan:")

	cbEmb := winc.NewCheckBox(mainWindow)
	cbEmb.SetPos(20, 75)
	cbEmb.SetSize(400, 22)
	cbEmb.SetText("Embedder Evaluation (Graph vs Raw)")
	cbEmb.SetChecked(true)

	cbRAG := winc.NewCheckBox(mainWindow)
	cbRAG.SetPos(20, 100)
	cbRAG.SetSize(400, 22)
	cbRAG.SetText("RAG Evaluation (Graph RAG vs Raw RAG)")
	cbRAG.SetChecked(true)

	cbLLM := winc.NewCheckBox(mainWindow)
	cbLLM.SetPos(20, 125)
	cbLLM.SetSize(400, 22)
	cbLLM.SetText("LLM Evaluation (All Groq models)")
	cbLLM.SetChecked(true)

	runBtn := winc.NewPushButton(mainWindow)
	runBtn.SetPos(20, 160)
	runBtn.SetSize(180, 32)
	runBtn.SetText("▶  RUN EVALUATION")

	stopBtn := winc.NewPushButton(mainWindow)
	stopBtn.SetPos(210, 160)
	stopBtn.SetSize(100, 32)
	stopBtn.SetText("🛑 STOP")
	stopBtn.SetEnabled(false)

	clearBtn := winc.NewPushButton(mainWindow)
	clearBtn.SetPos(320, 160)
	clearBtn.SetSize(120, 32)
	clearBtn.SetText("Clear Log")

	openOutputBtn := winc.NewPushButton(mainWindow)
	openOutputBtn.SetPos(450, 160)
	openOutputBtn.SetSize(150, 32)
	openOutputBtn.SetText("📁 Open Output")

	logEdit := winc.NewMultiEdit(mainWindow)
	logEdit.SetPos(20, 200)
	logEdit.SetSize(850, 460)

	// High-performance Win32 direct-append (same technique as embed_knowledge_base.go)
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

	var (
		mu         sync.Mutex
		running    bool
		cancelTask context.CancelFunc
	)

	setRunningState := func(isRunning bool) {
		running = isRunning
		runBtn.SetEnabled(!isRunning)
		cbEmb.SetEnabled(!isRunning)
		cbRAG.SetEnabled(!isRunning)
		cbLLM.SetEnabled(!isRunning)
		stopBtn.SetEnabled(isRunning)
	}

	runEval := func() {
		mu.Lock()
		if running {
			mu.Unlock()
			winc.MsgBoxOk(mainWindow, "Info", "Evaluasi sedang berjalan...")
			return
		}
		mu.Unlock()

		logEdit.SetText("")

		args := []string{"run_evaluation.py"}
		if !cbEmb.Checked() {
			args = append(args, "--skip-embedder")
		}
		if !cbRAG.Checked() {
			args = append(args, "--skip-rag")
		}
		if !cbLLM.Checked() {
			args = append(args, "--skip-llm")
		}

		ctx, cancel := context.WithCancel(context.Background())
		mu.Lock()
		cancelTask = cancel
		mu.Unlock()

		appendLog(fmt.Sprintf("[EXEC] python %v\r\n", args))
		setRunningState(true)

		go func() {
			defer func() {
				mu.Lock()
				running = false
				mu.Unlock()
				setRunningState(false)
				if ctx.Err() != nil {
					appendLog("\r\n[!] Dihentikan oleh user.")
				} else {
					appendLog("\r\n✅ SELESAI.")
				}
			}()

			cmd := exec.CommandContext(ctx, "python", args...)
			cmd.SysProcAttr = &syscall.SysProcAttr{
				HideWindow:    true,
				CreationFlags: 0x08000000, // CREATE_NO_WINDOW
			}
			cmd.Env = append(os.Environ(),
				"PYTHONUNBUFFERED=1",
				"PYTHONIOENCODING=utf-8",
			)

			stdout, _ := cmd.StdoutPipe()
			stderr, _ := cmd.StderrPipe()
			if err := cmd.Start(); err != nil {
				appendLog("ERROR: " + err.Error())
				return
			}

			stream := func(r io.Reader) {
				buf := make([]byte, 4096)
				var lineBuf strings.Builder
				for {
					n, err := r.Read(buf)
					if n > 0 {
						chunk := string(buf[:n])
						for _, ch := range chunk {
							if ch == '\n' {
								appendLog(strings.TrimRight(lineBuf.String(), "\r"))
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
					appendLog(lineBuf.String())
				}
			}

			var wg sync.WaitGroup
			wg.Add(2)
			go func() { defer wg.Done(); stream(stdout) }()
			go func() { defer wg.Done(); stream(stderr) }()
			wg.Wait()
			cmd.Wait()
		}()
	}

	runBtn.OnClick().Bind(func(e *winc.Event) { runEval() })

	stopBtn.OnClick().Bind(func(e *winc.Event) {
		mu.Lock()
		defer mu.Unlock()
		if cancelTask != nil {
			appendLog("\r\n[!] Cancelling process...")
			cancelTask()
		}
	})

	clearBtn.OnClick().Bind(func(e *winc.Event) {
		logEdit.SetText("")
	})

	openOutputBtn.OnClick().Bind(func(e *winc.Event) {
		exec.Command("explorer", "output").Start()
	})

	mainWindow.OnClose().Bind(func(e *winc.Event) {
		mu.Lock()
		if cancelTask != nil {
			cancelTask()
		}
		mu.Unlock()
		winc.Exit()
	})

	mainWindow.Center()
	mainWindow.Show()
	winc.RunMainLoop()
}