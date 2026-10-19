//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

const CREATE_NO_WINDOW = 0x08000000

// killProcessTree membunuh proses beserta seluruh anak-cucunya di Windows.
// taskkill /T = tree (ikut anak-cucu), /F = force.
func killProcessTree(pid int) error {
	if pid <= 0 {
		return nil
	}
	cmd := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(pid))
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: CREATE_NO_WINDOW,
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		msg := strings.ToLower(strings.TrimSpace(string(out)))
		// Proses sudah mati duluan — bukan error nyata.
		if strings.Contains(msg, "not found") ||
			strings.Contains(msg, "tidak ditemukan") ||
			strings.Contains(msg, "no running instance") {
			return nil
		}
		return fmt.Errorf("taskkill failed: %v (%s)", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// setProcAttr menyiapkan attribute proses untuk Windows.
// Tidak ada Setpgid di Windows — cukup sembunyikan window.
func setProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: CREATE_NO_WINDOW,
	}
}