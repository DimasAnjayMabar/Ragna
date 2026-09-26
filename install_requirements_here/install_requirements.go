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

	"github.com/tadvi/winc"
)

// Win32 API setup for log auto-scrolling without extra dependencies
var (
	user32           = syscall.NewLazyDLL("user32.dll")
	procSendMessageW = user32.NewProc("SendMessageW")
)

const (
	WM_VSCROLL     = 0x0115
	EM_SETSEL      = 0x00B1
	EM_SCROLLCARET = 0x00B7
	SB_BOTTOM      = 7
)

func sendMessage(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procSendMessageW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

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

	// Helper function to append text and auto-scroll to bottom using native syscalls
	appendLog := func(msg string) {
		current := logEdit.Text()
		logEdit.SetText(current + msg + "\r\n")

		textLen := uintptr(len(logEdit.Text()))
		sendMessage(logEdit.Handle(), EM_SETSEL, textLen, textLen)
		sendMessage(logEdit.Handle(), EM_SCROLLCARET, 0, 0)
		sendMessage(logEdit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
	}

	var cancelInstall context.CancelFunc

	installBtn.OnClick().Bind(func(e *winc.Event) {
		installBtn.SetEnabled(false)
		stopBtn.SetEnabled(true)
		osCombo.SetEnabled(false)
		pytorchCombo.SetEnabled(false)
		logEdit.SetText("")

		ctx, cancel := context.WithCancel(context.Background())
		cancelInstall = cancel

		go func() {
			defer func() {
				installBtn.SetEnabled(true)
				stopBtn.SetEnabled(false)
				osCombo.SetEnabled(true)
				pytorchCombo.SetEnabled(true)
			}()

			reqFile := reqFileInput.Text()
			selectedOptionLabel := pytorchCombo.Text()
			torchOpt, exists := optionsMap[selectedOptionLabel]
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

			appendLog("\r\n[✓] ALL INSTALLATIONS COMPLETED SUCCESSFULLY!")
		}()
	})

	stopBtn.OnClick().Bind(func(e *winc.Event) {
		if cancelInstall != nil {
			appendLog("\r\n[!] Stopping installation process...")
			cancelInstall()
		}
	})

	mainWindow.OnClose().Bind(func(e *winc.Event) {
		if cancelInstall != nil {
			cancelInstall()
		}
		winc.Exit()
	})

	mainWindow.Show()
	mainWindow.Center()
	winc.RunMainLoop()
}

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

func ensureVenv(ctx context.Context, logFn func(string)) (string, error) {
	venvDir := filepath.Join("..", ".venv")
	var venvPython string

	if runtime.GOOS == "windows" {
		venvPython = filepath.Join(venvDir, "Scripts", "python.exe")
	} else {
		venvPython = filepath.Join(venvDir, "bin", "python")
	}

	if _, err := os.Stat(venvDir); os.IsNotExist(err) {
		logFn("[>] .venv directory not found in root (..). Creating virtual environment...")
		systemPython := "python"
		if runtime.GOOS != "windows" {
			systemPython = "python3"
		}
		if err := runCommand(ctx, logFn, systemPython, "-m", "venv", venvDir); err != nil {
			return "", fmt.Errorf("failed to create virtualenv in parent dir: %w", err)
		}
		logFn("[✓] Created virtual environment in root (.venv)")
	} else {
		logFn("[✓] Existing root .venv directory found.")
	}

	return venvPython, nil
}

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

	return cmd.Wait()
}