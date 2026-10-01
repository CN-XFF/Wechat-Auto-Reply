#include "Version.iss"
#define AppName "微信自动回复助手"
#define AppExeName "WeChatAutoReply.exe"
#define BuildDir "output\" + AppVersion + "\WeChatAutoReply"

[Setup]
AppId={{7F980E67-1CA9-4DB4-A5D2-E96F747A1CC0}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\WeChatAutoReply
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=release\{#AppVersion}
OutputBaseFilename=WeChatAutoReply-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
VersionInfoVersion=1.0.9.0
VersionInfoProductName={#AppName}
VersionInfoDescription=Windows WeChat text auto-reply assistant

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildDir}\_internal\config.example.json"; DestDir: "{app}\_internal"; DestName: "config.json"; Flags: onlyifdoesntexist
Source: "{#BuildDir}\docs\CODEX_AFTER_INSTALL_HANDOFF.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "INSTALLATION_GUIDE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\启动 {#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\编辑本机配置"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\_internal\config.json"""
Name: "{group}\重新配置微信与联系人"; Filename: "{app}\{#AppExeName}"; Parameters: "--configure"; WorkingDir: "{app}"
Name: "{group}\安装与配置说明"; Filename: "{app}\docs\INSTALLATION_GUIDE.md"
Name: "{group}\交给 Codex 的配置说明"; Filename: "{app}\docs\CODEX_AFTER_INSTALL_HANDOFF.md"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动程序并完成首次配置"; Flags: postinstall nowait skipifsilent
