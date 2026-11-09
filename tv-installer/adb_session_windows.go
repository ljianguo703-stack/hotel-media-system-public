package main

import (
	"os"
	"os/exec"
	"syscall"
)

func isolateADBWorker(cmd *exec.Cmd) {
	// CREATE_NEW_PROCESS_GROUP keeps a console Ctrl-C from terminating ADB before a
	// running install/rollback finishes. The owning application's pipe still controls it.
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x00000200}
}

func installerSignals() []os.Signal { return []os.Signal{os.Interrupt, syscall.SIGTERM} }
