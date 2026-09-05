fn main() {
    const ICON: &str = "../frontend/src-tauri/icons/icon.ico";
    println!("cargo:rerun-if-changed={ICON}");

    // Launcher 是安装后的用户入口，必须自行嵌入资源；Tauri 不会为 companion 添加图标。
    // 与 UI 共用图标源，资源编译失败时直接终止构建，避免产出无图标的安装包。
    if std::env::var("CARGO_CFG_TARGET_OS").unwrap() == "windows" {
        tauri_winres::WindowsResource::new()
            .set_icon(ICON)
            .compile()
            .expect("failed to compile Launcher Windows icon resources");
    }
}
