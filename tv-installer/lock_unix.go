//go:build darwin || linux

package main

import (
	"errors"
	"os"
	"syscall"
)

func lockState(path string) (*os.File, error) {
	f, e := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		f.Close()
		return nil, errors.New("本店数据目录已有工具运行，请使用已打开的窗口或先关闭原程序")
	}
	return f, nil
}
