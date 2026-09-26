package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// =============================================================================
// TYPES
// =============================================================================

type ModelData struct {
	ModelID      string `json:"model_id"`
	Name         string `json:"name"`
	Provider     string `json:"provider"`
	TPS          int    `json:"tps"`
	TPMLimit     int    `json:"tpm_limit"`
	Tier         string `json:"tier"`
	Description  string `json:"description"`
	IsCustomDesc bool   `json:"is_custom_desc"`
}

// =============================================================================
// GLOBAL STATE
// =============================================================================

var (
	configPath string
	lastPing   atomic.Int64 // unix nano timestamp
)

const (
	port           = "8089"
	pingTimeout    = 30 * time.Second
	pingCheckEvery = 5 * time.Second
	shutdownGrace  = 3 * time.Second
)

// =============================================================================
// MAIN
// =============================================================================

func main() {
	locateProjectFiles()

	mux := http.NewServeMux()
	mux.HandleFunc("/", handleIndex)
	mux.HandleFunc("/api/get-models", handleGetModels)
	mux.HandleFunc("/api/apply", handleApply)
	mux.HandleFunc("/api/ping", handlePing)
	mux.HandleFunc("/api/shutdown", handleShutdown)

	srv := &http.Server{
		Addr:    ":" + port,
		Handler: mux,
	}

	lastPing.Store(time.Now().UnixNano())

	// ── Auto-shutdown jika tidak ada ping selama pingTimeout ─────────────
	go func() {
		ticker := time.NewTicker(pingCheckEvery)
		defer ticker.Stop()
		for range ticker.C {
			last := time.Unix(0, lastPing.Load())
			if time.Since(last) > pingTimeout {
				log.Println("[auto-shutdown] No ping received for", pingTimeout, "— shutting down.")
				shutdownServer(srv)
				return
			}
		}
	}()

	// ── Signal handler: Ctrl+C / SIGTERM ─────────────────────────────────
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("[signal] Received shutdown signal.")
		shutdownServer(srv)
	}()

	url := "http://localhost:" + port
	go func() {
		time.Sleep(300 * time.Millisecond)
		openBrowser(url)
	}()

	log.Println("Groq Model Manager running on", url)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}
	log.Println("Server stopped.")
}

func shutdownServer(srv *http.Server) {
	ctx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Println("[shutdown] Forced close:", err)
		_ = srv.Close()
	}
	// Pastikan proses benar-benar keluar
	os.Exit(0)
}

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("cmd", "/c", "start", "", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}

// =============================================================================
// FILE LOCATOR
// =============================================================================

// locateProjectFiles cari config.py dengan menaiki direktori — baik dari
// working directory saat tool dijalankan MAUPUN dari lokasi file binary itu
// sendiri. Ini penting karena working directory berbeda-beda tergantung cara
// menjalankan tool (double-click .exe, `go run` dari root, dari terminal di
// dalam folder change_groq_llm_model_here/, dst).
func locateProjectFiles() {
	if wd, err := os.Getwd(); err == nil {
		if p := findConfigUpwards(wd); p != "" {
			configPath = p
			log.Println("config.py     →", configPath, "(via working directory)")
			return
		}
	}

	if exe, err := os.Executable(); err == nil {
		if p := findConfigUpwards(filepath.Dir(exe)); p != "" {
			configPath = p
			log.Println("config.py     →", configPath, "(via executable location)")
			return
		}
	}

	configPath = "../config.py"
	log.Println("[warn] config.py TIDAK ditemukan otomatis — pakai fallback:", configPath)
	log.Println("[warn] Jika ini salah, jalankan tool dari dalam folder project, atau")
	log.Println("[warn] pindahkan config.py sehingga ada di working directory / parent-nya.")
}

// findConfigUpwards menaiki maksimal 5 level direktori dari start mencari config.py.
func findConfigUpwards(start string) string {
	dir := start
	for i := 0; i < 5; i++ {
		candidate := filepath.Join(dir, "config.py")
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return ""
}

// =============================================================================
// HTTP HANDLERS
// =============================================================================

func handlePing(w http.ResponseWriter, r *http.Request) {
	lastPing.Store(time.Now().UnixNano())
	w.WriteHeader(http.StatusNoContent)
}

func handleShutdown(w http.ResponseWriter, r *http.Request) {
	w.Write([]byte("shutting down"))
	go func() {
		time.Sleep(200 * time.Millisecond)
		log.Println("[shutdown] Browser requested shutdown.")
		os.Exit(0)
	}()
}

func handleGetModels(w http.ResponseWriter, r *http.Request) {
	models := []ModelData{}

	content, err := os.ReadFile(configPath)
	if err != nil {
		msg := fmt.Sprintf("cannot read config.py at %s: %v", configPath, err)
		log.Println("[get-models]", msg)
		writeJSONWithWarning(w, models, msg)
		return
	}
	src := string(content)

	// Satu-satunya source of truth: GROQ_MODEL_REGISTRY = { "model-id": {...}, ... }
	registryBlock := extractBlockContent(src, "GROQ_MODEL_REGISTRY")
	if registryBlock == "" {
		var msg string
		if strings.Contains(src, "GROQ_ALLOWED_MODELS") {
			msg = fmt.Sprintf("config.py at %s is still the OLD format (has GROQ_ALLOWED_MODELS/GROQ_MODEL_TPM_LIMITS but no GROQ_MODEL_REGISTRY yet). Replace it with the new config.py.", configPath)
		} else {
			msg = fmt.Sprintf("GROQ_MODEL_REGISTRY block not found in %s", configPath)
		}
		log.Println("[get-models]", msg)
		writeJSONWithWarning(w, models, msg)
		return
	}

	models = parseModelRegistry(registryBlock)

	log.Printf("[get-models] Loaded %d models from GROQ_MODEL_REGISTRY (%s)\n", len(models), configPath)
	writeJSON(w, models)
}

// writeJSONWithWarning mengembalikan {"models": [...], "warning": "..."} agar
// UI bisa menampilkan alasan kenapa daftar kosong, alih-alih diam saja.
func writeJSONWithWarning(w http.ResponseWriter, models []ModelData, warning string) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"models":  models,
		"warning": warning,
	})
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	// Jika v adalah nil slice dari type apapun, encode sebagai []
	// (tapi karena kita sudah inisialisasi `models := []ModelData{}`, ini jarang terpakai)
	if v == nil {
		w.Write([]byte("[]"))
		return
	}
	_ = json.NewEncoder(w).Encode(v)
}

// extractBlockContent mencari `identifier = {` lalu mengembalikan
// isi di dalam `{...}` (tanpa brace). Return "" jika tidak ditemukan.
func extractBlockContent(src, identifier string) string {
	re := regexp.MustCompile(`(?m)^\s*` + regexp.QuoteMeta(identifier) + `\s*=\s*`)
	loc := re.FindStringIndex(src)
	if loc == nil {
		return ""
	}

	openIdx := strings.IndexByte(src[loc[1]:], '{')
	if openIdx == -1 {
		return ""
	}
	openIdx += loc[1]

	closeIdx := findBlockEnd(src, openIdx, '{', '}')
	if closeIdx == -1 || closeIdx <= openIdx {
		return ""
	}
	return src[openIdx+1 : closeIdx]
}

// parseModelRegistry mengekstrak tiap entry "model-id": { name, provider,
// tier, description, tps, tpm_limit } dari blok GROQ_MODEL_REGISTRY.
func parseModelRegistry(registryBlock string) []ModelData {
	models := []ModelData{}
	if registryBlock == "" {
		return models
	}

	reKey := regexp.MustCompile(`"([^"]+)"\s*:\s*\{`)
	locs := reKey.FindAllStringSubmatchIndex(registryBlock, -1)

	reName := regexp.MustCompile(`"name"\s*:\s*"([^"]*)"`)
	reProvider := regexp.MustCompile(`"provider"\s*:\s*"([^"]*)"`)
	reTier := regexp.MustCompile(`"tier"\s*:\s*"([^"]*)"`)
	reDesc := regexp.MustCompile(`"description"\s*:\s*"([^"]*)"`)
	reTPS := regexp.MustCompile(`"tps"\s*:\s*(\d+)`)
	reTPM := regexp.MustCompile(`"tpm_limit"\s*:\s*([\d_]+)`)

	for _, loc := range locs {
		modelID := registryBlock[loc[2]:loc[3]]
		closeIdx := findBlockEnd(registryBlock, loc[1]-1, '{', '}')
		if closeIdx == -1 {
			continue
		}
		inner := registryBlock[loc[1]-1 : closeIdx+1]

		tps := 100
		if m := reTPS.FindStringSubmatch(inner); m != nil {
			tps, _ = strconv.Atoi(m[1])
		}
		tpm := 6000
		if m := reTPM.FindStringSubmatch(inner); m != nil {
			tpm, _ = strconv.Atoi(strings.ReplaceAll(m[1], "_", ""))
		}
		tier := calculateTierFromTPS(tps)
		if m := reTier.FindStringSubmatch(inner); m != nil && m[1] != "" {
			tier = m[1]
		}
		desc := fmt.Sprintf("%s tier model at %d tps", tier, tps)
		isCustom := false
		if m := reDesc.FindStringSubmatch(inner); m != nil && m[1] != "" {
			desc = m[1]
			isCustom = true
		}
		name := modelID
		if m := reName.FindStringSubmatch(inner); m != nil && m[1] != "" {
			name = m[1]
		}
		provider := "Groq"
		if m := reProvider.FindStringSubmatch(inner); m != nil && m[1] != "" {
			provider = m[1]
		}

		models = append(models, ModelData{
			ModelID:      modelID,
			Name:         name,
			Provider:     provider,
			TPS:          tps,
			TPMLimit:     tpm,
			Tier:         tier,
			Description:  desc,
			IsCustomDesc: isCustom,
		})
	}
	return models
}

func calculateTierFromTPS(tps int) string {
	if tps >= 800 {
		return "fast"
	}
	if tps >= 400 {
		return "medium"
	}
	return "large"
}

func handleApply(w http.ResponseWriter, r *http.Request) {
	var models []ModelData
	if err := json.NewDecoder(r.Body).Decode(&models); err != nil {
		http.Error(w, err.Error(), 400)
		return
	}

	// ── Build satu blok GROQ_MODEL_REGISTRY baru ──────────────────────────
	// Ini satu-satunya blok yang perlu ditulis. GROQ_ALLOWED_MODELS,
	// GROQ_MODEL_TPM_LIMITS, list_local_models() (semua di config.py) dan
	// _get_model_metadata() (di controller_chats.py) diturunkan otomatis
	// dari dict ini saat Python di-restart — tidak perlu di-patch lagi.
	registryLines := []string{"{"}

	validCount := 0
	for _, m := range models {
		id := strings.TrimSpace(m.ModelID)
		if id == "" {
			continue
		}
		validCount++

		name := strings.TrimSpace(m.Name)
		if name == "" {
			name = id
		}
		provider := strings.TrimSpace(m.Provider)
		if provider == "" {
			provider = "Groq"
		}
		tier := m.Tier
		if tier == "" {
			tier = calculateTierFromTPS(m.TPS)
		}
		desc := m.Description
		if desc == "" {
			desc = fmt.Sprintf("%s tier model at %d tps", tier, m.TPS)
		}
		tpm := m.TPMLimit
		if tpm <= 0 {
			tpm = 6000
		}

		registryLines = append(registryLines,
			fmt.Sprintf(`    "%s": {`, id),
			fmt.Sprintf(`        "name": %q,`, name),
			fmt.Sprintf(`        "provider": %q,`, provider),
			fmt.Sprintf(`        "tier": %q,`, tier),
			fmt.Sprintf(`        "description": %q,`, desc),
			fmt.Sprintf(`        "tps": %d,`, m.TPS),
			fmt.Sprintf(`        "tpm_limit": %d,`, tpm),
			`    },`,
		)
	}

	if validCount == 0 {
		http.Error(w, "No valid models provided.", 400)
		return
	}

	registryLines = append(registryLines, "}")
	registryBlock := strings.Join(registryLines, "\n")

	// ── Patch config.py (satu-satunya file yang perlu ditulis) ────────────
	cfgBytes, err := os.ReadFile(configPath)
	if err != nil {
		http.Error(w, "cannot read config.py: "+err.Error(), 500)
		return
	}
	cfgStr := string(cfgBytes)
	cfgStr = replaceBlockByBraceBalance(cfgStr, "GROQ_MODEL_REGISTRY", registryBlock)

	if err := os.WriteFile(configPath, []byte(cfgStr), 0644); err != nil {
		http.Error(w, "cannot write config.py: "+err.Error(), 500)
		return
	}

	log.Printf("[apply] Updated %d models → GROQ_MODEL_REGISTRY in config.py\n", validCount)
	w.Write([]byte("OK"))
}

// =============================================================================
// BRACE-BALANCING PARSER
// =============================================================================
//
// Menggantikan regex non-greedy `.*?` yang berhenti di `}` pertama —
// yang menyebabkan indentasi rusak ketika ada nested dict di dalam blok.
//
// Parser ini:
//  1. Cari `identifier =` (atau `def identifier`)
//  2. Cari `{` atau `[` pertama setelahnya
//  3. Hitung depth dengan skip string / komentar
//  4. Return posisi closing yang benar

// findBlockEnd mencari posisi closing brace yang match dengan opening pertama
// setelah startPos. Return index closing, atau -1 jika tidak ditemukan.
func findBlockEnd(s string, startPos int, open, close byte) int {
	openIdx := strings.IndexByte(s[startPos:], open)
	if openIdx == -1 {
		return -1
	}
	openIdx += startPos

	depth := 0
	inString := false
	var quoteChar byte
	escaped := false
	inLineComment := false
	inBlockComment := false

	for i := openIdx; i < len(s); i++ {
		c := s[i]

		if inString {
			if escaped {
				escaped = false
				continue
			}
			if c == '\\' {
				escaped = true
				continue
			}
			if c == quoteChar {
				inString = false
			}
			continue
		}

		if inLineComment {
			if c == '\n' {
				inLineComment = false
			}
			continue
		}

		if inBlockComment {
			if c == '*' && i+1 < len(s) && s[i+1] == '/' {
				inBlockComment = false
				i++
			}
			continue
		}

		// Deteksi string
		if c == '"' || c == '\'' {
			inString = true
			quoteChar = c
			continue
		}

		// Deteksi komentar Python (#) dan C-style (// /*)
		if c == '#' {
			inLineComment = true
			continue
		}
		if c == '/' && i+1 < len(s) {
			if s[i+1] == '/' {
				inLineComment = true
				i++
				continue
			}
			if s[i+1] == '*' {
				inBlockComment = true
				i++
				continue
			}
		}

		if c == open {
			depth++
		} else if c == close {
			depth--
			if depth == 0 {
				return i
			}
		}
	}
	return -1
}

// replaceBlockByBraceBalance mengganti blok `identifier = { ... }` dengan
// newContent. Cocok untuk dict assignment.
func replaceBlockByBraceBalance(src, identifier, newContent string) string {
	re := regexp.MustCompile(`(?m)^\s*` + regexp.QuoteMeta(identifier) + `\s*=\s*`)
	loc := re.FindStringIndex(src)
	if loc == nil {
		log.Printf("[warn] identifier '%s' not found — skipping.\n", identifier)
		return src
	}

	closeIdx := findBlockEnd(src, loc[1], '{', '}')
	if closeIdx == -1 {
		log.Printf("[warn] closing brace for '%s' not found — skipping.\n", identifier)
		return src
	}

	lineStart := strings.LastIndexByte(src[:loc[0]], '\n') + 1

	// Perpanjang ke akhir baris setelah closing brace (untuk konsumsi newline)
	after := closeIdx + 1
	if after < len(src) && src[after] == '\n' {
		after++
	}

	return src[:lineStart] + identifier + " = " + newContent + "\n" + src[after:]
}

// =============================================================================
// INDEX HTML (frontend)
// =============================================================================

func handleIndex(w http.ResponseWriter, r *http.Request) {
	html := `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Groq Model Manager</title>
<style>
body { font-family: system-ui, sans-serif; padding: 20px; background: #f8f9fa; }
h2 { margin-top: 0; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
th, td { padding: 12px; border: 1px solid #e9ecef; text-align: left; }
th { background: #f1f3f5; }
input { width: 95%; padding: 8px; border: 1px solid #ced4da; border-radius: 4px; box-sizing: border-box; }
button { padding: 8px 16px; border-radius: 4px; border: none; cursor: pointer; font-weight: bold; }
.btn-add { background: #0d6efd; color: white; }
.btn-apply { background: #198754; color: white; margin-left: 8px; }
.btn-del { background: #dc3545; color: white; }
.hint { color: #6c757d; font-size: 0.9em; margin-top: 6px; }
</style>
</head>
<body>
<h2>Groq Model Configuration Manager</h2>
<p class="hint">Server auto-shutdown 30 detik setelah semua tab ditutup.</p>
<p>
  <button class="btn-add" onclick="addRow()">+ Add Model Row</button>
  <button class="btn-apply" onclick="applyChanges()">Apply & Rewrite Files</button>
</p>
<div id="warning" style="display:none; background:#fff3cd; border:1px solid #ffe69c; color:#664d03; padding:10px 14px; border-radius:6px; margin-bottom:12px; font-size:0.9em;"></div>
<table id="tbl">
<thead><tr><th>Model ID</th><th>Display Name</th><th>Provider</th><th>TPS</th><th>TPM Limit</th><th>Tier (Auto)</th><th>Description</th><th>Action</th></tr></thead>
<tbody></tbody>
</table>
<script>
let models = [];

async function load() {
  try {
    const res = await fetch('/api/get-models');
    const data = await res.json();
    const warningEl = document.getElementById('warning');
    if (Array.isArray(data)) {
      // Format lama: langsung array (kompatibilitas)
      models = data;
      warningEl.style.display = 'none';
    } else if (data && Array.isArray(data.models)) {
      models = data.models;
      if (data.warning) {
        warningEl.textContent = '⚠️ ' + data.warning;
        warningEl.style.display = 'block';
      } else {
        warningEl.style.display = 'none';
      }
    } else {
      models = [];
      warningEl.textContent = '⚠️ Unexpected response from /api/get-models.';
      warningEl.style.display = 'block';
    }
    render();
  } catch (e) {
    console.error('load failed', e);
    models = [];
    const warningEl = document.getElementById('warning');
    warningEl.textContent = '⚠️ Failed to reach the tool server: ' + e.message;
    warningEl.style.display = 'block';
    render();
  }
}

function calculateTier(tps) {
  return tps >= 800 ? 'fast' : (tps >= 400 ? 'medium' : 'large');
}

function updateTPS(i, val) {
  let tps = parseInt(val) || 0;
  models[i].tps = tps;
  models[i].tier = calculateTier(tps);
  if (!models[i].is_custom_desc) {
    models[i].description = models[i].tier + " tier model at " + tps + " tps";
  }
  render();
}

function updateDesc(i, val) {
  models[i].description = val;
  models[i].is_custom_desc = true;
}

function addRow() {
  models.push({
    model_id: '',
    name: '',
    provider: 'Groq',
    tps: 500,
    tpm_limit: 6000,
    tier: 'medium',
    description: 'medium tier model at 500 tps',
    is_custom_desc: false
  });
  render();
}

function delRow(i) {
  models.splice(i, 1);
  render();
}

function render() {
  const tbody = document.querySelector('#tbl tbody');
  tbody.innerHTML = '';
  // Safety: pastikan models adalah array
  if (!Array.isArray(models)) models = [];
  if (models.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#888;">' +
      'Tidak ada model. Klik "+ Add Model Row" untuk menambahkan.</td></tr>';
    return;
  }
  models.forEach((m, i) => {
    tbody.innerHTML += ` + "`" + `<tr>
      <td><input value="${m.model_id}" oninput="models[${i}].model_id=this.value"></td>
      <td><input value="${m.name || ''}" oninput="models[${i}].name=this.value"></td>
      <td><input value="${m.provider || ''}" oninput="models[${i}].provider=this.value"></td>
      <td><input type="number" value="${m.tps}" oninput="updateTPS(${i}, this.value)"></td>
      <td><input type="number" value="${m.tpm_limit || 6000}" oninput="models[${i}].tpm_limit=parseInt(this.value)||6000"></td>
      <td><b>${m.tier}</b></td>
      <td><input value="${m.description}" oninput="updateDesc(${i}, this.value)"></td>
      <td><button class="btn-del" onclick="delRow(${i})">Delete</button></td>
    </tr>` + "`" + `;
  });
}

async function applyChanges() {
  try {
    let res = await fetch('/api/apply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(models)
    });
    if (res.ok) {
      alert('Successfully updated GROQ_MODEL_REGISTRY in config.py!\nRestart the backend for pipeline.py and controller_chats.py to pick it up.');
    } else {
      let txt = await res.text();
      alert('Failed: ' + txt);
    }
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Heartbeat: kirim ping tiap 10 detik ─────────────────────────────────
function sendPing() {
  fetch('/api/ping', { method: 'POST', keepalive: true }).catch(() => {});
}
sendPing();
setInterval(sendPing, 10000);

// ── Shutdown saat tab ditutup ───────────────────────────────────────────
window.addEventListener('pagehide', () => {
  if (navigator.sendBeacon) {
    navigator.sendBeacon('/api/shutdown');
  } else {
    fetch('/api/shutdown', { method: 'POST', keepalive: true });
  }
});

// ── Fallback visibility: jika tab hidden >5 menit → shutdown ────────────
let hiddenTimer = null;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    hiddenTimer = setTimeout(() => {
      if (navigator.sendBeacon) navigator.sendBeacon('/api/shutdown');
    }, 5 * 60 * 1000);
  } else {
    if (hiddenTimer) { clearTimeout(hiddenTimer); hiddenTimer = null; }
    sendPing();
  }
});

load();
</script>
</body>
</html>`
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(html))
}