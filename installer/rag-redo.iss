; Inno Setup 安装包脚本——Phase 4 最后一块拼图（见 ../docs/ROADMAP.md）。
;
; 跑法（Windows 上装好 Inno Setup 6 之后）：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\rag-redo.iss
; 前提：先跑过 installer/build_windows.py，dist\rag-redo-gui\ 和
; dist\rag-redo-mcp\ 两个 PyInstaller onedir 产物已经存在。
;
; 设计决策（旧项目 obsidian-rag 从来没有做过安装包，这是这一轮真正的新
; 设计决策，不是照抄旧项目行为）：
;   1. **不要求管理员权限**（PrivilegesRequired=lowest）——目标用户"完全
;      不懂编程"，弹 UAC 提权对话框本身就是一道不必要的门槛，装到当前
;      用户自己的目录（{autopf}在没有管理员权限时 Inno Setup 会自动
;      降级成用户目录）比要求管理员权限更符合"下载→双击→能用"。
;   2. **GUI 和 MCP 两个冻结产物装进同一个 {app} 目录下的不同子文件夹**
;      （gui\ / mcp\），避免两份 PyInstaller onedir 各自的 _internal
;      运行时目录互相覆盖——这是 PyInstaller onedir 打包的已知限制，不是
;      这份脚本手滑。两边共享同一份用户数据：gui_main.py/mcp_stdio.py
;      冻结时都把 DATA_ROOT 指向 %LOCALAPPDATA%\RAG-Redo\data（不是各自
;      安装目录下的 data\），所以哪怕分装在不同子文件夹，操作的仍然是
;      同一批已建索引的库，见两个入口脚本里 DATA_ROOT 常量的注释。
;   3. **只注册开始菜单快捷方式和卸载入口**，不碰注册表其他位置、不改
;      全局 PATH——AGENTS.md 架构红线7"Windows 安装包例外：只注册自己
;      的开始菜单快捷方式和卸载入口，卸载时清理干净"的字面落实。
;   4. **卸载不删除 %LOCALAPPDATA%\RAG-Redo\data**——Inno Setup 默认只
;      删除它自己往 {app} 目录里装的文件，用户已经建好的索引库数据不在
;      {app} 目录下（见第2点），卸载程序体不会碰到它，这是刻意的设计
;      （不是"忘了清理"）：卸载=删掉这个程序，不等于用户想清空自己积累
;      的检索索引，这条数据丢失比"卸载后残留几十MB数据"的代价大得多。

#define MyAppName "RAG Redo"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "RAG Redo"
#define MyAppExeName "rag-redo-gui.exe"

[Setup]
AppId={{8C9B5C9C-6E3B-4E2A-9E0A-1B7D6F2C4A9F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=rag-redo-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 便携版 onedir 产物本身就没有签名，安装包这一层也先不签——个人/小规模
; 分发场景签名证书的成本收益不成比例，用户第一次运行时 Windows
; SmartScreen 提示"未知发布者"是预期行为，不是这份脚本的缺陷。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
; 简体中文语言包（ChineseSimplified.isl）不是 Inno Setup 官方随编译器
; 自带的文件，需要从第三方渠道单独下载——为了不在安装包脚本里引入一个
; 未经充分核实来源的第三方翻译文件（供应链风险，装好之后会被同一个
; 安装向导原样执行），这一版先只用编译器自带的英文，是刻意的范围收窄，
; 不是遗漏；真要中文向导界面，后续可以在确认好官方翻译文件来源之后
; 单独补上。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; 见文件头设计决策第2点：分装进不同子文件夹，避免两份 onedir 的
; _internal 互相覆盖。/e 之外用 Excludes 排除任何测试残留的 __pycache__
; （dist/ 里理论上不该有，防御性排除不算多余）。
Source: "..\dist\rag-redo-gui\*"; DestDir: "{app}\gui"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__"
Source: "..\dist\rag-redo-mcp\*"; DestDir: "{app}\mcp"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__"
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.en.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\gui\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\gui\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Run]
Filename: "{app}\gui\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清理安装目录本身（Inno Setup 默认行为），不主动触碰
; %LOCALAPPDATA%\RAG-Redo\data——见文件头设计决策第4点，这里显式留白
; 不是遗漏，是"卸载程序不等于清空用户数据"这条决策的直接体现。
