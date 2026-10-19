//go:build windows
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
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/tadvi/winc"
)

// Win32 API setup for log auto-scrolling
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

type PyEnv struct {
	PythonCmd  string
	AlembicCmd string
	IsValid    bool
}

func main() {
	mainWindow := winc.NewForm(nil)
	mainWindow.SetSize(820, 680)
	mainWindow.SetText("Alembic Auto Migration Tool")

	workDir, _ := filepath.Abs(".")

	// Control Buttons Layout
	btnCheckEnv := winc.NewPushButton(mainWindow)
	btnCheckEnv.SetPos(20, 15)
	btnCheckEnv.SetSize(180, 32)
	btnCheckEnv.SetText("🔍 Check Environment")

	btnSetup := winc.NewPushButton(mainWindow)
	btnSetup.SetPos(210, 15)
	btnSetup.SetSize(180, 32)
	btnSetup.SetText("⚙️ 1. Setup Alembic")

	btnInit := winc.NewPushButton(mainWindow)
	btnInit.SetPos(400, 15)
	btnInit.SetSize(180, 32)
	btnInit.SetText("📦 2. Init Baseline")

	btnGenerate := winc.NewPushButton(mainWindow)
	btnGenerate.SetPos(20, 55)
	btnGenerate.SetSize(180, 32)
	btnGenerate.SetText("🔄 3. Generate Migration")

	btnUpgrade := winc.NewPushButton(mainWindow)
	btnUpgrade.SetPos(210, 55)
	btnUpgrade.SetSize(180, 32)
	btnUpgrade.SetText("⬆️ 4. Apply Migration")

	btnDowngrade := winc.NewPushButton(mainWindow)
	btnDowngrade.SetPos(400, 55)
	btnDowngrade.SetSize(180, 32)
	btnDowngrade.SetText("⬇️ Downgrade -1")

	btnHistory := winc.NewPushButton(mainWindow)
	btnHistory.SetPos(20, 95)
	btnHistory.SetSize(180, 32)
	btnHistory.SetText("📜 Show History")

	btnCurrent := winc.NewPushButton(mainWindow)
	btnCurrent.SetPos(210, 95)
	btnCurrent.SetSize(180, 32)
	btnCurrent.SetText("📍 Current Revision")

	btnClear := winc.NewPushButton(mainWindow)
	btnClear.SetPos(400, 95)
	btnClear.SetSize(180, 32)
	btnClear.SetText("🗑️ Clear Log")

	// Stop Button
	btnStop := winc.NewPushButton(mainWindow)
	btnStop.SetPos(590, 15)
	btnStop.SetSize(190, 112)
	btnStop.SetText("🛑 STOP")
	btnStop.SetEnabled(false)

	// Console Log Area
	logEdit := winc.NewMultiEdit(mainWindow)
	logEdit.SetPos(20, 140)
	logEdit.SetSize(760, 480)

	// ── Rolling log buffer ─────────────────────────────────────────
	// Win32 MultiEdit punya batas ukuran teks (bisa terpotong kalau
	// terlalu panjang). Kita simpan hanya MAX_LOG_LINES baris terakhir
	// di memori, dan rebuild kontrol hanya saat buffer melebihi batas.
	// Di antara rebuild, append biasa supaya tetap cepat.
	const MaxLogLines = 5000
	const TrimBatch   = 500 // buang sekian baris sekaligus saat limit tercapai

	var logMu sync.Mutex
	logLines := make([]string, 0, MaxLogLines+TrimBatch)
	logNeedsRebuild := false

	scrollToBottom := func() {
		textLen := uintptr(len(logEdit.Text()))
		sendMessage(logEdit.Handle(), EM_SETSEL, textLen, textLen)
		sendMessage(logEdit.Handle(), EM_SCROLLCARET, 0, 0)
		sendMessage(logEdit.Handle(), WM_VSCROLL, SB_BOTTOM, 0)
	}

	appendLog := func(msg string) {
		logMu.Lock()
		defer logMu.Unlock()

		// Pecah per baris supaya hitungan baris akurat (msg bisa multi-line).
		for _, ln := range strings.Split(msg, "\n") {
			ln = strings.TrimRight(ln, "\r")
			logLines = append(logLines, ln)
		}

		// Kalau melebihi batas, buang yang paling lama sekaligus (batch)
		// supaya tidak rebuild tiap 1 baris.
		if len(logLines) > MaxLogLines+TrimBatch {
			drop := len(logLines) - MaxLogLines
			logLines = append(logLines[:0], logLines[drop:]...)
			logNeedsRebuild = true
		}

		if logNeedsRebuild {
			// Rebuild seluruh kontrol dari buffer (jarang terjadi).
			logEdit.SetText(strings.Join(logLines, "\r\n"))
			logNeedsRebuild = false
		} else {
			// Append incremental (cepat).
			current := logEdit.Text()
			logEdit.SetText(current + msg + "\r\n")
		}

		scrollToBottom()
	}

	allButtons := []*winc.PushButton{
		btnCheckEnv, btnSetup, btnInit, btnGenerate,
		btnUpgrade, btnDowngrade, btnHistory, btnCurrent, btnClear,
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

	btnSetup.OnClick().Bind(func(e *winc.Event) {
		runAsync("Setup Alembic", func(ctx context.Context) error {
			return setupAlembic(workDir, appendLog)
		})
	})

	btnInit.OnClick().Bind(func(e *winc.Event) {
		runAsync("Init Baseline", func(ctx context.Context) error {
			return initBaseline(workDir, appendLog)
		})
	})

	btnGenerate.OnClick().Bind(func(e *winc.Event) {
		runAsync("Generate Migration", func(ctx context.Context) error {
			appendLog("Running: alembic revision --autogenerate -m \"auto migration\"")
			return runAlembicCommand(ctx, appendLog, workDir, "revision", "--autogenerate", "-m", "auto migration")
		})
	})

	btnUpgrade.OnClick().Bind(func(e *winc.Event) {
		runAsync("Apply Migration", func(ctx context.Context) error {
			appendLog("Running: alembic upgrade head")
			return runAlembicCommand(ctx, appendLog, workDir, "upgrade", "head")
		})
	})

	btnDowngrade.OnClick().Bind(func(e *winc.Event) {
		runAsync("Downgrade -1", func(ctx context.Context) error {
			appendLog("Running: alembic downgrade -1")
			return runAlembicCommand(ctx, appendLog, workDir, "downgrade", "-1")
		})
	})

	btnHistory.OnClick().Bind(func(e *winc.Event) {
		runAsync("Show History", func(ctx context.Context) error {
			return runAlembicCommand(ctx, appendLog, workDir, "history", "--verbose")
		})
	})

	btnCurrent.OnClick().Bind(func(e *winc.Event) {
		runAsync("Show Current Revision", func(ctx context.Context) error {
			return runAlembicCommand(ctx, appendLog, workDir, "current", "--verbose")
		})
	})

	btnClear.OnClick().Bind(func(e *winc.Event) {
		logMu.Lock()
		logLines = logLines[:0]
		logNeedsRebuild = false
		logMu.Unlock()
		logEdit.SetText("")
	})

	btnStop.OnClick().Bind(func(e *winc.Event) {
		if cancelTask != nil {
			appendLog("\r\n[!] Cancelling current process tree...")
			cancelTask()
		}
	})

	mainWindow.OnClose().Bind(func(e *winc.Event) {
		if cancelTask != nil {
			cancelTask()
			// Beri waktu singkat agar kill process tree sempat berjalan
			time.Sleep(300 * time.Millisecond)
		}
		winc.Exit()
	})

	mainWindow.Show()
	mainWindow.Center()
	winc.RunMainLoop()
}

// ==================== PYTHON & ALEMBIC RESOLVER ====================

func resolvePythonEnv() PyEnv {
	venvDirs := []string{
		".env",                             // migrate_here/.venv
		filepath.Join("..", ".venv"),       // backend/.venv
		filepath.Join("..", "..", ".venv"), // root/.venv
	}

	// 1. Search for virtual environment with all required packages
	for _, venv := range venvDirs {
		pyPath := filepath.Join(venv, "Scripts", "python.exe")
		alembicPath := filepath.Join(venv, "Scripts", "alembic.exe")

		if !fileExists(pyPath) {
			pyPath = filepath.Join(venv, "bin", "python")
			alembicPath = filepath.Join(venv, "bin", "alembic")
		}

		if fileExists(pyPath) {
			absPy, _ := filepath.Abs(pyPath)
			absAlembic, _ := filepath.Abs(alembicPath)

			cmd := exec.Command(absPy, "-c", "import alembic, sqlalchemy, pymysql")
			setProcAttr(cmd)
			if err := cmd.Run(); err == nil {
				cmdAlembic := absAlembic
				if !fileExists(absAlembic) {
					cmdAlembic = absPy
				}
				return PyEnv{PythonCmd: absPy, AlembicCmd: cmdAlembic, IsValid: true}
			}
		}
	}

	// 2. Check system PATH
	if absPy, err := exec.LookPath("python"); err == nil {
		cmd := exec.Command(absPy, "-c", "import alembic, sqlalchemy, pymysql")
		setProcAttr(cmd)
		if err := cmd.Run(); err == nil {
			if absAlembic, err := exec.LookPath("alembic"); err == nil {
				return PyEnv{PythonCmd: absPy, AlembicCmd: absAlembic, IsValid: true}
			}
			return PyEnv{PythonCmd: absPy, AlembicCmd: absPy, IsValid: true}
		}
	}

	// 3. Fallback to first existing venv found
	for _, venv := range venvDirs {
		pyPath := filepath.Join(venv, "Scripts", "python.exe")
		alembicPath := filepath.Join(venv, "Scripts", "alembic.exe")
		if fileExists(pyPath) {
			absPy, _ := filepath.Abs(pyPath)
			absAlembic, _ := filepath.Abs(alembicPath)
			return PyEnv{PythonCmd: absPy, AlembicCmd: absAlembic, IsValid: false}
		}
	}

	if runtime.GOOS == "windows" {
		return PyEnv{PythonCmd: "python", AlembicCmd: "alembic", IsValid: false}
	}
	return PyEnv{PythonCmd: "python3", AlembicCmd: "alembic", IsValid: false}
}

func runAlembicCommand(ctx context.Context, logFn func(string), workDir string, args ...string) error {
	env := resolvePythonEnv()

	if strings.HasSuffix(strings.ToLower(env.AlembicCmd), "alembic.exe") || strings.HasSuffix(strings.ToLower(env.AlembicCmd), "alembic") {
		if fileExists(env.AlembicCmd) {
			return runCommand(ctx, logFn, workDir, env.AlembicCmd, args...)
		}
	}

	fullArgs := append([]string{"-m", "alembic.config"}, args...)
	return runCommand(ctx, logFn, workDir, env.PythonCmd, fullArgs...)
}

// ==================== CORE FUNCTIONS ====================

func getDatabaseURL() (string, error) {
	paths := []string{
		".env",
		filepath.Join("..", ".env"),
		filepath.Join("..", "..", ".env"),
	}

	for _, path := range paths {
		fileBytes, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		lines := strings.Split(string(fileBytes), "\n")
		for _, raw := range lines {
			line := strings.TrimSpace(raw)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			// support "export DATABASE_URL=..."
			line = strings.TrimPrefix(line, "export ")
			if !strings.Contains(line, "=") {
				continue
			}
			parts := strings.SplitN(line, "=", 2)
			key := strings.TrimSpace(parts[0])
			if !strings.EqualFold(key, "DATABASE_URL") {
				continue
			}
			val := strings.TrimSpace(parts[1])
			val = strings.Trim(val, `"'`)
			if val == "" {
				return "", fmt.Errorf("DATABASE_URL is empty in %s", path)
			}
			return val, nil
		}
	}

	absCwd, _ := filepath.Abs(".")
	return "", fmt.Errorf(
		"DATABASE_URL not found in .env (searched: .env, ../.env, ../../.env relative to %s)",
		absCwd,
	)
}

func maskDBPassword(url string) string {
	// cari "://"
	i := strings.Index(url, "://")
	if i < 0 {
		return url
	}
	rest := url[i+3:]
	// cari "@" pertama setelah scheme
	at := strings.Index(rest, "@")
	if at < 0 {
		return url
	}
	userinfo := rest[:at]
	colon := strings.Index(userinfo, ":")
	if colon < 0 {
		return url // tidak ada password
	}
	return url[:i+3] + userinfo[:colon] + ":***" + rest[at:]
}

// neo4jCheckScript melakukan koneksi nyata ke server Neo4j (bukan sekadar import package).
// Jika tidak bisa connect (server mati / port salah), exit code != 0 sehingga runCommand
// mengembalikan error dan log menampilkan ❌.
const neo4jCheckScript = `
import os, sys
from pathlib import Path

# ── Cari .env di cwd, parent, grandparent, great-grandparent ──
def load_dotenv():
    here = Path.cwd()
    for base in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        envfile = base / ".env"
        if envfile.is_file():
            print("neo4j: loading %s" % envfile)
            try:
                text = envfile.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = envfile.read_text(encoding="utf-8", errors="replace")
            for raw in text.splitlines():
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
    uri  = os.environ.get("NEO4J_URI", "") or os.environ.get("NEO4J_URL", "")
    user = os.environ.get("NEO4J_USER", "") or os.environ.get("NEO4J_USERNAME", "")
    pw   = os.environ.get("NEO4J_PASS", "") or os.environ.get("NEO4J_PASSWORD", "")
    if not uri or not user or not pw:
        print("neo4j FAIL: NEO4J_URI / NEO4J_USER / NEO4J_PASS not set in .env")
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

func checkEnvironment(ctx context.Context, workDir string, logFn func(string)) error {
	env := resolvePythonEnv()
	logFn(fmt.Sprintf("[check] Target folder: %s", workDir))
	logFn(fmt.Sprintf("[check] Python binary: %s", env.PythonCmd))
	logFn(fmt.Sprintf("[check] Alembic binary: %s", env.AlembicCmd))

	hasError := false

	checks := []struct {
		name string
		cmd  string
		args []string
	}{
		{"Python", env.PythonCmd, []string{"--version"}},
		{"PyMySQL", env.PythonCmd, []string{"-c", "import pymysql; print('pymysql OK')"}},
		{"SQLAlchemy", env.PythonCmd, []string{"-c", "import sqlalchemy; print('sqlalchemy', sqlalchemy.__version__)"}},
		// Neo4j: koneksi nyata, bukan import saja
		{"Neo4j", env.PythonCmd, []string{"-c", neo4jCheckScript}},
	}

	for _, c := range checks {
		logFn(fmt.Sprintf("\n[check] Checking %s...", c.name))
		if err := runCommand(ctx, logFn, workDir, c.cmd, c.args...); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			logFn(fmt.Sprintf("  ❌ %s unavailable", c.name))
			hasError = true
		}
	}

	logFn("\n[check] Checking Alembic CLI...")
	if err := runAlembicCommand(ctx, logFn, workDir, "--version"); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		logFn("  ❌ Alembic unavailable")
		hasError = true
	}

	if hasError {
		logFn(fmt.Sprintf("\n💡 Run this command in terminal to fix:\n   & \"%s\" -m pip install alembic pymysql sqlalchemy neo4j", env.PythonCmd))
	}

	modelsPath := filepath.Join(workDir, "models.py")
	if !fileExists(modelsPath) {
		modelsPath = filepath.Join(workDir, "..", "models.py")
	}

	if absModels, err := filepath.Abs(modelsPath); err == nil && fileExists(absModels) {
		logFn(fmt.Sprintf("\n[check] models.py found: %s", absModels))
	} else {
		logFn("\n[check] ❌ models.py not found in migrate_here or backend folder")
	}

	logFn("\n[check] Reading DATABASE_URL from .env...")
	dbURL, err := getDatabaseURL()
	if err != nil {
		logFn(fmt.Sprintf("  ❌ %v", err))
		hasError = true
	} else {
		// Sembunyikan password kalau ada (user:pass@host)
		logFn(fmt.Sprintf("  ✅ Database URL: %s", maskDBPassword(dbURL)))
	}

	if hasError {
		logFn("\n⚠️  Environment has issues — fix the ❌ items above before running migrations.")
	}
	return nil
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// setupAlembic selalu melakukan RESET PENUH folder alembic/ + alembic.ini di workDir.
// Cocok untuk fresh clone dari GitHub: jika folder lama masih ada (mis. sisa commit
// yang menyertakan migrasi), akan dihapus agar tidak terjadi race condition antara
// file migrasi lama dengan database yang masih kosong.
func setupAlembic(workDir string, logFn func(string)) error {
	// 1) Hapus legacy duplikat di parent folder
	parentIni := filepath.Join(workDir, "..", "alembic.ini")
	parentAlembic := filepath.Join(workDir, "..", "alembic")
	if fileExists(parentIni) {
		if err := os.Remove(parentIni); err == nil {
			logFn("🧹 Removed legacy duplicate: ../alembic.ini")
		}
	}
	if fileExists(parentAlembic) {
		if err := os.RemoveAll(parentAlembic); err == nil {
			logFn("🧹 Removed legacy duplicate folder: ../alembic/")
		}
	}

	// 2) Reset penuh folder alembic/ + alembic.ini di workDir
	localAlembic := filepath.Join(workDir, "alembic")
	localIni := filepath.Join(workDir, "alembic.ini")

	if fileExists(localAlembic) {
		if err := os.RemoveAll(localAlembic); err != nil {
			return fmt.Errorf("failed to reset alembic folder: %w", err)
		}
		logFn("🧹 Reset: removed existing alembic/ folder (fresh start).")
	}
	if fileExists(localIni) {
		if err := os.Remove(localIni); err != nil {
			return fmt.Errorf("failed to reset alembic.ini: %w", err)
		}
		logFn("🧹 Reset: removed existing alembic.ini.")
	}

	// 3) Buat ulang dari nol
	versionsDir := filepath.Join(localAlembic, "versions")
	if err := os.MkdirAll(versionsDir, 0755); err != nil {
		return fmt.Errorf("failed to create alembic folder: %w", err)
	}
	logFn("📁 Fresh folder alembic/ and alembic/versions/ created inside migrate_here.")

	// 4) Tulis alembic.ini
	dbURL, err := getDatabaseURL()
	if err != nil {
		// Rollback: hapus folder yang baru dibuat supaya tidak setengah jadi
		_ = os.RemoveAll(localAlembic)
		_ = os.Remove(localIni)
		return fmt.Errorf("cannot setup alembic: %w", err)
	}
	iniContent := buildAlembicIni(dbURL)
	if err := os.WriteFile(localIni, []byte(iniContent), 0644); err != nil {
		return fmt.Errorf("failed to write alembic.ini: %w", err)
	}
	logFn(fmt.Sprintf("📝 alembic.ini created in migrate_here with URL: %s", dbURL))

	// 5) Tulis env.py
	envPath := filepath.Join(localAlembic, "env.py")
	if err := os.WriteFile(envPath, []byte(buildEnvPy()), 0644); err != nil {
		return fmt.Errorf("failed to write env.py: %w", err)
	}
	logFn("📝 alembic/env.py created.")

	// 6) Tulis script.py.mako
	makoPath := filepath.Join(localAlembic, "script.py.mako")
	if err := os.WriteFile(makoPath, []byte(buildScriptMako()), 0644); err != nil {
		return fmt.Errorf("failed to write script.py.mako: %w", err)
	}
	logFn("📝 alembic/script.py.mako created.")

	// 7) README
	readmePath := filepath.Join(localAlembic, "README")
	_ = os.WriteFile(readmePath, []byte("Single-database migration configuration for migrate_here.\n"), 0644)

	logFn("\n✅ Alembic setup completed (fresh reset) in migrate_here folder.")
	return nil
}

// initBaseline melakukan RESET baseline: hapus semua file migrasi di versions/
// agar sinkron dengan DB yang masih kosong. Tidak lagi skip ketika sudah ada file.
func initBaseline(workDir string, logFn func(string)) error {
	versionsDir := filepath.Join(workDir, "alembic", "versions")
	if !fileExists(versionsDir) {
		return fmt.Errorf("alembic/versions not found, run Setup Alembic first")
	}

	entries, err := os.ReadDir(versionsDir)
	if err != nil {
		return fmt.Errorf("cannot read versions dir: %w", err)
	}

	removed := 0
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, ".py") || name == "__pycache__" {
			p := filepath.Join(versionsDir, name)
			if err := os.RemoveAll(p); err != nil {
				logFn(fmt.Sprintf("⚠️  Failed to remove %s: %v", p, err))
				continue
			}
			logFn(fmt.Sprintf("🧹 Removed: versions/%s", name))
			removed++
		}
	}

	if removed > 0 {
		logFn(fmt.Sprintf("\n✅ Baseline reset: %d item(s) removed from versions/.", removed))
	} else {
		logFn("ℹ️  versions/ already empty — baseline is clean.")
	}

	logFn("➡️  Next: Step 3 (Generate Migration) then Step 4 (Apply).")
	return nil
}

// ==================== TEMPLATES ====================

func buildAlembicIni(dbURL string) string {
	return fmt.Sprintf(`# Alembic Config — auto-generated in migrate_here
[alembic]
script_location = alembic
prepend_sys_path = .
version_path_separator = os
sqlalchemy.url = %s

[post_write_hooks]

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %%%% (levelname)-5.5s [%%%%(name)s] %%%% (message)s
datefmt = %%%% H:%%%% M:%%%% S
`, dbURL)
}

func buildEnvPy() string {
	return `# Alembic env.py — auto-generated
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
migrate_here_dir = os.path.dirname(current_dir)
backend_dir = os.path.dirname(migrate_here_dir)

sys.path.insert(0, migrate_here_dir)
sys.path.insert(0, backend_dir)

try:
    from models import Base
except ImportError:
    from migrate_here.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
`
}

func buildScriptMako() string {
	return `"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
`
}

// ==================== STREAMING EXEC COMMAND ====================

func runCommand(ctx context.Context, logFn func(string), workDir, name string, args ...string) error {
	// JANGAN pakai exec.CommandContext — kita kelola kill sendiri agar
	// seluruh process tree ikut mati, bukan hanya proses utama.
	cmd := exec.Command(name, args...)
	if workDir != "" {
		cmd.Dir = workDir
	}

	// Abstraksi per-OS (lihat proctree_windows.go / proctree_unix.go):
	// Windows → HideWindow + CREATE_NO_WINDOW;
	// Unix    → Setpgid agar kill(-pid) mengenai seluruh process group.
	setProcAttr(cmd)

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

	pid := cmd.Process.Pid

	// Watcher: jika ctx dibatalkan, kill seluruh process tree.
	watcherDone := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			logFn("[!] Killing process tree (pid " + strconv.Itoa(pid) + ")...")
			if err := killProcessTree(pid); err != nil {
				logFn(fmt.Sprintf("[!] kill tree error: %v", err))
			}
		case <-watcherDone:
			// proses selesai normal, tidak perlu kill
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

	waitErr := cmd.Wait()
	close(watcherDone)

	// Jika user menekan Stop, jangan laporkan sebagai error nyata.
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return waitErr
}