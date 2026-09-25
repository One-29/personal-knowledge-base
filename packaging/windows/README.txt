KnowBase Windows 桌面版
======================

1. 双击 KnowBase.exe 启动。
2. 首次使用模型前，双击 “Configure KnowBase.cmd”，填写 EMBEDDING_API_KEY
   和 LLM_API_KEY，保存后重新启动 KnowBase。
3. 数据库、原文、配置和日志默认保存在：
   %LOCALAPPDATA%\KnowBase

本目录可以整体移动，但不要只复制 KnowBase.exe；_internal 目录是应用运行时的
一部分。升级前正常关闭 KnowBase，再用新版目录替换旧版目录。用户数据不在程序
目录中，不会因替换程序而删除。

系统要求：64 位 Windows 10/11 与 Microsoft Edge WebView2 Runtime。
未签名的个人开源构建可能触发 Windows SmartScreen；请只从项目 GitHub Releases
下载，并核对发布页提供的 SHA-256。
