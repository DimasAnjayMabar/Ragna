//go:build !windows

package main

import (
	"os/exec"
	"syscall"
)

// killProcessTree membunuh proses group di Unix.
// Butuh Setpgid: true saat Start agar -pid valid.
func killProcessTree(pid int) error {
	if pid <= 0 {
		return nil
	}
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil {
		if err == syscall.ESRCH {
			return nil
		}
		return err
	}
	return nil
}

// setProcAttr membuat process group baru agar kill(-pid) mengenai semua anak.
func setProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}