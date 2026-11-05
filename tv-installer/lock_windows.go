//go:build windows

package main

import (
	"errors"
	"os"
	"syscall"
	"unsafe"
)

func lockState(path string) (*os.File, error) {
	f, e := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	overlap := syscall.Overlapped{}
	fn := syscall.NewLazyDLL("kernel32.dll").NewProc("LockFileEx")
	ok, _, _ := fn.Call(f.Fd(), 3, 0, 1, 0, uintptr(unsafe.Pointer(&overlap)))
	if ok == 0 {
		f.Close()
		return nil, errors.New("本店数据目录已有工具运行，请使用已打开的窗口或先关闭原程序")
	}
	return f, nil
}
