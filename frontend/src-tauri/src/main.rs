// 防止 Windows release 模式出现额外控制台窗口，请勿移除！
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    harness_shell_lib::run()
}
